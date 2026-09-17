"""纸张预设、参考层、笔位对齐的测试。

覆盖：
    * 纸张预设（内置 A4、方向切换、用户预设增删改与持久化）
    * 参考层模型（包围盒、适应、序列化）与文档管理
    * 参考层不参与 G-code 输出
    * 坐标反变换与笔位标记
    * 参考层面板 / 画布 / 主窗口的 UI 行为
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from writerstudio.core.document import Document, PageSpec, make_static_object  # noqa: E402
from writerstudio.core.geometry import AffineTransform, BBox  # noqa: E402
from writerstudio.core.reference import (  # noqa: E402
    KIND_IMAGE,
    KIND_SVG,
    ReferenceItem,
    fit_reference_to_box,
)
from writerstudio.core.sample import make_rect  # noqa: E402
from writerstudio.machine.config import GCodeConfig  # noqa: E402
from writerstudio.machine.gcode_gen import generate_from_document  # noqa: E402
from writerstudio.machine.start_point import StartPoint  # noqa: E402
from writerstudio.page_presets import (  # noqa: E402
    PagePreset,
    PagePresetStore,
    _iso_a,
    builtin_presets,
)
from writerstudio.project import (  # noqa: E402
    ProjectData,
    load_project,
    project_from_data,
    project_to_data,
    save_project,
)
from writerstudio.settings import Settings, _MemoryBackend  # noqa: E402

pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from writerstudio.ui.controller import DocumentController  # noqa: E402
from writerstudio.ui.items import (  # noqa: E402
    Handle,
    ReferenceGraphicsItem,
)
from writerstudio.ui.main_window import MainWindow  # noqa: E402
from writerstudio.ui.page_dialog import PagePresetEditorDialog, PageSetupDialog  # noqa: E402
from writerstudio.ui.reference_panel import make_reference_from_file  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


def _mem_settings() -> Settings:
    """内存后端，避免测试污染用户真实配置。"""
    return Settings(_MemoryBackend())


# ============================================================ 纸张预设
def test_iso_a4_dimensions():
    w, h = _iso_a(4)
    assert (round(w), round(h)) == (210, 297)


def test_builtin_presets_include_a4_and_letter():
    names = {p.name for p in builtin_presets()}
    assert "A4" in names and "A0" in names and "Letter" in names
    a4 = next(p for p in builtin_presets() if p.name == "A4")
    assert (a4.width, a4.height) == (210.0, 297.0)
    assert a4.builtin


def test_preset_oriented_switches():
    a4 = PagePreset("A4", 210.0, 297.0)
    assert a4.portrait()
    assert a4.oriented(landscape=True) == (297.0, 210.0)
    assert a4.oriented(landscape=False) == (210.0, 297.0)
    square = PagePreset("S", 100.0, 100.0)
    assert square.oriented(landscape=True) == (100.0, 100.0)


def test_preset_store_user_crud_and_persistence():
    backend = _MemoryBackend()
    s = Settings(backend)
    store = PagePresetStore(s)
    assert store.find("A4") is not None
    assert store.is_builtin("A4")

    mine = PagePreset("我的纸张", 123.0, 456.0, 7.0)
    store.add(mine)
    assert store.find("我的纸张") is not None
    assert not store.is_builtin("我的纸张")

    # 新实例应能从同一后端恢复
    store2 = PagePresetStore(Settings(backend))
    assert store2.find("我的纸张") is not None
    assert store2.find("我的纸张").width == 123.0

    # 内置不可删除，用户可删除
    assert store2.remove("A4") is False
    assert store2.remove("我的纸张") is True
    assert store2.find("我的纸张") is None


def test_preset_store_update_rename():
    store = PagePresetStore(Settings(_MemoryBackend()))
    store.add(PagePreset("甲", 100.0, 200.0))
    store.update("甲", PagePreset("乙", 110.0, 210.0))
    assert store.find("甲") is None
    assert store.find("乙").height == 210.0
    # 内置改名 → 另存为用户预设
    store.update("A4", PagePreset("宽 A4", 300.0, 210.0))
    assert store.find("宽 A4") is not None
    assert store.find("A4") is not None


def test_preset_store_rejects_builtin_name():
    """用户预设不得占用内置名（回归：曾出现两个同名 A4）。"""
    store = PagePresetStore(Settings(_MemoryBackend()))
    with pytest.raises(ValueError):
        store.add(PagePreset("A4", 100.0, 100.0))
    assert [p.name for p in store.all()].count("A4") == 1
    # 内置同名 update 不应生效也不应报错
    store.update("A4", PagePreset("A4", 100.0, 100.0))
    assert store.find("A4").width == 210.0


def test_page_setup_dialog_clears_stale_preset_name(qapp):
    """预设名失效（已删除）时应回退为自定义，而不是保留无效名字。"""
    from writerstudio.ui.page_dialog import PageSetupDialog
    store = PagePresetStore(Settings(_MemoryBackend()))
    dlg = PageSetupDialog(PageSpec(297, 210, 10, preset_name="不存在的预设"), store)
    assert dlg.result_preset_name() == ""
    assert dlg.preset_combo.currentIndex() == 0


def test_reference_panel_keeps_selection_on_opacity_change(qapp):
    """调透明度会触发 documentChanged；不应因此清空列表选择（回归）。"""
    from writerstudio.ui.reference_panel import ReferencePanel
    ctrl = DocumentController(Document())
    a = ReferenceItem(name="a", width_mm=50, height_mm=30)
    b = ReferenceItem(name="b", width_mm=60, height_mm=40)
    ctrl.add_reference(a)
    ctrl.add_reference(b)
    panel = ReferencePanel(ctrl)
    panel.list.setCurrentRow(0)
    assert [r.name for r in panel.selected_refs()] == ["a"]
    ctrl.documentChanged.connect(panel.refresh_if_changed)
    panel.opacity.setValue(80)
    # 选集仍在，且透明度已应用
    assert [r.name for r in panel.selected_refs()] == ["a"]
    assert a.opacity == pytest.approx(0.8)
    # 结构变化（新增）后仍保留原选择
    ctrl.add_reference(ReferenceItem(name="c", width_mm=10, height_mm=10))
    panel.refresh_if_changed()
    assert [r.name for r in panel.selected_refs()] == ["a"]


def test_page_margin_bbox():
    page = PageSpec(width=210.0, height=297.0, margin=15.0)
    mb = page.margin_bbox()
    assert mb.as_tuple() == (15.0, 15.0, 195.0, 282.0)


def test_page_preset_name_roundtrip():
    doc = Document(page=PageSpec(210, 297, 10, preset_name="A4"))
    data = project_to_data(ProjectData(document=doc))
    back = project_from_data(data)
    assert back.document.page.preset_name == "A4"


# ============================================================ 参考层模型
def test_reference_bbox_and_transform():
    ref = ReferenceItem(width_mm=100.0, height_mm=50.0,
                        transform=AffineTransform.translate(10.0, 20.0))
    assert ref.local_bbox().as_tuple() == (0.0, 0.0, 100.0, 50.0)
    assert ref.world_bbox().as_tuple() == (10.0, 20.0, 110.0, 70.0)


def test_reference_roundtrip():
    ref = ReferenceItem(name="图", kind=KIND_SVG, path="/tmp/a.svg",
                        width_mm=30.0, height_mm=40.0, opacity=0.3, locked=True)
    back = ReferenceItem.from_data(ref.to_data())
    assert back.kind == KIND_SVG and back.path == "/tmp/a.svg"
    assert back.opacity == pytest.approx(0.3) and back.locked


def test_fit_reference_keeps_aspect_and_centers():
    ref = ReferenceItem(width_mm=200.0, height_mm=100.0)
    fit_reference_to_box(ref, BBox(0.0, 0.0, 100.0, 100.0))
    assert ref.width_mm == pytest.approx(100.0)
    assert ref.height_mm == pytest.approx(50.0)
    # 居中：y 中心应在 50
    wb = ref.world_bbox()
    assert wb.center[0] == pytest.approx(50.0)
    assert wb.center[1] == pytest.approx(50.0)


def test_document_reference_management():
    doc = Document()
    a = ReferenceItem(width_mm=10, height_mm=10)
    b = ReferenceItem(width_mm=20, height_mm=20)
    doc.add_reference(a)
    doc.add_reference(b)
    assert doc.find_reference(a.id) is a
    assert doc.references_bbox().as_tuple() == (0.0, 0.0, 20.0, 20.0)
    b.visible = False
    assert doc.references_bbox().as_tuple() == (0.0, 0.0, 10.0, 10.0)
    idx = doc.remove_reference(a)
    assert idx == 0 and len(doc.references) == 1


def test_references_excluded_from_gcode():
    doc = Document()
    doc.add(make_static_object([make_rect(0, 0, 10, 10)]))
    doc.add_reference(ReferenceItem(width_mm=200, height_mm=200))
    r = generate_from_document(doc, GCodeConfig())
    assert r.stroke_count == 1     # 参考层不产生笔画


def test_references_serialization_roundtrip(tmp_path):
    doc = Document()
    doc.add(make_static_object([make_rect(0, 0, 5, 5)]))
    doc.add_reference(ReferenceItem(name="底图", path="/x/y.png",
                                    width_mm=50, height_mm=25, opacity=0.6))
    p = ProjectData(document=doc)
    path = tmp_path / "a.wsproj"
    save_project(path, p)
    back = load_project(path)
    assert len(back.document.references) == 1
    ref = back.document.references[0]
    assert ref.name == "底图" and ref.width_mm == 50
    assert ref.opacity == pytest.approx(0.6)


def test_make_reference_from_image_file(qapp, tmp_path):
    from PySide6.QtGui import QColor, QImage
    img = QImage(120, 60, QImage.Format_ARGB32)
    img.fill(QColor("white"))
    path = tmp_path / "shot.png"
    assert img.save(str(path))
    ref = make_reference_from_file(str(path))
    assert ref.kind == KIND_IMAGE
    assert ref.width_mm > 0 and ref.height_mm > 0
    # 宽高比保持
    assert ref.width_mm / ref.height_mm == pytest.approx(2.0, rel=0.05)


def test_reference_image_uses_embedded_dpi(qapp, tmp_path):
    """300dpi 的 A4 扫描件应换算成约 210×297mm（回归：曾按 96dpi 得到 656×928）。"""
    from PySide6.QtGui import QColor, QImage
    img = QImage(2480, 3508, QImage.Format_ARGB32)   # A4 @ 300dpi
    img.setDotsPerMeterX(int(300 / 0.0254))
    img.setDotsPerMeterY(int(300 / 0.0254))
    img.fill(QColor("white"))
    path = tmp_path / "scan300.png"
    assert img.save(str(path))
    ref = make_reference_from_file(str(path))
    assert ref.width_mm == pytest.approx(210.0, abs=1.0)
    assert ref.height_mm == pytest.approx(297.0, abs=1.0)


def test_reference_missing_or_corrupt_file_fallback(qapp, tmp_path):
    """缺失/损坏文件不应抛异常，退化为默认尺寸并可显示占位框。"""
    missing = make_reference_from_file(str(tmp_path / "nope.png"))
    assert missing.width_mm == 100.0 and missing.height_mm == 100.0
    corrupt = tmp_path / "bad.png"
    corrupt.write_bytes(b"definitely not an image")
    c = make_reference_from_file(str(corrupt))
    assert c.width_mm == 100.0
    doc = Document()
    ctrl = DocumentController(doc)
    ctrl.add_reference(c)
    from writerstudio.ui.canvas import CanvasView
    view = CanvasView(ctrl)
    view.resize(400, 300)
    _keep_alive(view)
    item = view._ref_items[c.id]
    assert item._pix is None and item._load_error
    view.viewport().update()   # 渲染占位框不应报错


def test_make_reference_from_svg_file(qapp, tmp_path):
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20">'
           '<rect width="40" height="20" fill="black"/></svg>')
    path = tmp_path / "ref.svg"
    path.write_text(svg)
    ref = make_reference_from_file(str(path))
    assert ref.kind == KIND_SVG
    assert ref.width_mm > 0 and ref.height_mm > 0


def test_svg_physical_units_use_real_mm(qapp, tmp_path):
    """mm 单位的 SVG 应按真实毫米解析（回归：Qt 按 90dpi 会小 6%）。"""
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="210mm" height="297mm" '
           'viewBox="0 0 210 297"><rect width="210" height="297"/></svg>')
    path = tmp_path / "a4.svg"
    path.write_text(svg)
    ref = make_reference_from_file(str(path))
    assert ref.width_mm == pytest.approx(210.0, abs=0.1)
    assert ref.height_mm == pytest.approx(297.0, abs=0.1)
    # 其他单位
    svg2 = ('<svg xmlns="http://www.w3.org/2000/svg" width="21cm" height="8.27in"/>')
    p2 = tmp_path / "b.svg"
    p2.write_text(svg2)
    r2 = make_reference_from_file(str(p2))
    assert r2.width_mm == pytest.approx(210.0, abs=0.2)
    assert r2.height_mm == pytest.approx(210.06, abs=0.3)


def test_svg_natural_and_place_on_page(qapp, tmp_path):
    """「原尺寸贴页」应恢复到文件原始尺寸并对齐页面原点。"""
    from writerstudio.ui.reference_panel import ReferencePanel
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="210mm" height="297mm" '
           'viewBox="0 0 210 297"><rect width="210" height="297"/></svg>')
    path = tmp_path / "a4.svg"
    path.write_text(svg)
    doc = Document(page=PageSpec(210.0, 297.0, 10.0))
    ctrl = DocumentController(doc)
    ref = make_reference_from_file(str(path))
    # 模拟被缩放
    ref.width_mm, ref.height_mm = 100.0, 141.4
    ref.transform = AffineTransform.translate(30, 40)
    ctrl.add_reference(ref)
    panel = ReferencePanel(ctrl)
    panel.list.setCurrentRow(0)
    panel._place_natural()
    assert ref.width_mm == pytest.approx(210.0, abs=0.1)
    assert ref.world_bbox().as_tuple() == pytest.approx((0.0, 0.0, 210.0, 297.0))


def test_reference_item_natural_size_roundtrip():
    ref = ReferenceItem(width_mm=100, height_mm=50,
                        natural_width_mm=210, natural_height_mm=297)
    back = ReferenceItem.from_data(ref.to_data())
    assert back.natural_size() == (210.0, 297.0)
    # 未记录原尺寸时退回当前尺寸
    assert ReferenceItem(width_mm=80, height_mm=60).natural_size() == (80.0, 60.0)


def test_reference_image_orientation_not_flipped(qapp):
    """参考图不能上下颠倒（回归：本地 Y 向上 vs 位图 Y 向下）。"""
    from PySide6.QtGui import QColor, QImage, QPainter
    from writerstudio.ui.canvas import CanvasView
    import tempfile
    from pathlib import Path
    tmp = Path(tempfile.mkdtemp())
    img = QImage(100, 100, QImage.Format_ARGB32)
    img.fill(QColor("white"))
    p = QPainter(img)
    p.fillRect(0, 0, 100, 50, QColor(220, 0, 0))      # 图上半 = 红
    p.fillRect(0, 50, 100, 50, QColor(0, 0, 220))     # 图下半 = 蓝
    p.end()
    path = tmp / "half.png"
    img.save(str(path))
    ctrl = DocumentController(Document(page=PageSpec(200, 200)))
    ref = make_reference_from_file(str(path))
    ref.width_mm, ref.height_mm = 100.0, 100.0
    ref.transform = AffineTransform.translate(50, 50)
    ref.opacity = 1.0
    ctrl.add_reference(ref)
    view = CanvasView(ctrl)
    view.resize(400, 400)
    view.fit_page()
    _keep_alive(view)
    shot = QImage(view.viewport().size(), QImage.Format_ARGB32)
    shot.fill(QColor("white"))
    pp = QPainter(shot)
    view.render(pp)
    pp.end()

    def sample(sy):
        from PySide6.QtCore import QPointF
        pt = view.mapFromScene(QPointF(100, sy))
        return shot.pixelColor(pt.x(), pt.y()).name()

    assert sample(140) == "#dc0000"    # 场景上方 = 图的上半（红）
    assert sample(60) == "#0000dc"     # 场景下方 = 图的下半（蓝）


def test_report_template_imports_at_a4(qapp):
    """随附的实验报告模板应解析为 210×297mm。"""
    from pathlib import Path
    tpl = Path(__file__).resolve().parent.parent / "templates" / \
        "实验报告_A4_对齐模板.svg"
    if not tpl.exists():
        pytest.skip("模板文件不存在")
    ref = make_reference_from_file(str(tpl))
    assert ref.kind == KIND_SVG
    assert ref.width_mm == pytest.approx(210.0, abs=0.2)
    assert ref.height_mm == pytest.approx(297.0, abs=0.2)


# ============================================================ 坐标/笔位
def test_unmap_point_is_inverse_of_map():
    cfg = GCodeConfig(scale=2.0, mirror_x=True, origin_offset=(30.0, -12.0))
    p = (3.5, -7.25)
    assert cfg.unmap_point(cfg.map_point(p)) == pytest.approx(p)


def test_unmap_point_identity():
    cfg = GCodeConfig()
    assert cfg.unmap_point((5.0, 6.0)) == (5.0, 6.0)


def test_start_point_pen_marker_roundtrip():
    sp = StartPoint("canvas", (1.0, 2.0), pen_marker=(3.0, 4.0))
    back = StartPoint.from_data(sp.to_data())
    assert back.pen_marker == (3.0, 4.0)
    assert StartPoint.from_data({}).pen_marker is None


# ============================================================ UI
def test_canvas_creates_reference_item(qapp):
    doc = Document()
    ctrl = DocumentController(doc)
    from writerstudio.ui.canvas import CanvasView
    view = CanvasView(ctrl)
    ref = ReferenceItem(width_mm=50, height_mm=30)
    ctrl.add_reference(ref)
    assert ref.id in view._ref_items
    assert isinstance(view._ref_items[ref.id], ReferenceGraphicsItem)
    # 参考层 z 值应在书写对象之下
    assert view._ref_items[ref.id].zValue() < 0
    ctrl.remove_reference(ref)
    assert ref.id not in view._ref_items


def test_reference_item_drag_commits_undo(qapp):
    doc = Document()
    ctrl = DocumentController(doc)
    ref = ReferenceItem(width_mm=50, height_mm=30)
    ctrl.add_reference(ref)
    from writerstudio.ui.canvas import CanvasView
    view = CanvasView(ctrl)
    view.resize(600, 400)
    _keep_alive(view)
    item = view._ref_items[ref.id]
    original = ref.transform
    item._begin_drag('move', None, _FakeEvent((0, 0), (0, 0)))
    item._drag_kind = 'move'
    item._preview(AffineTransform.translate(7.0, 8.0) @ original)
    item.mouseReleaseEvent(_FakeEvent((0, 0), (0, 0)))
    assert ref.world_bbox().as_tuple()[0] == pytest.approx(7.0)
    ctrl.undo()
    assert ref.transform == original


# --------------------------------------------------- 缩放吸附纸张边缘（P23）
def _page_ref_item(ctrl, w=100.0, h=80.0):
    ref = ReferenceItem(width_mm=w, height_mm=h)
    ctrl.add_reference(ref)
    from writerstudio.ui.canvas import CanvasView
    view = CanvasView(ctrl)
    view.resize(600, 400)
    _keep_alive(view)
    return ref, view._ref_items[ref.id]


def test_resize_snap_to_outer_page_edge(qapp):
    """拖动右缘靠近纸边（297mm）→ 吸附到纸边。"""
    ctrl = DocumentController(Document())        # A4 297×210，边距 10
    ref, item = _page_ref_item(ctrl)
    item._start_transform = ref.transform
    from PySide6.QtCore import QPointF
    sx, sy = item._resize_snap(Handle.R, QPointF(0, 40), 2.96, 1.0)
    assert sx * 100.0 == pytest.approx(297.0)
    assert sy == 1.0


def test_resize_snap_to_margin_line(qapp):
    """靠近页边距内框（287mm）→ 吸附到内框而不是外边缘。"""
    ctrl = DocumentController(Document())
    ref, item = _page_ref_item(ctrl)
    item._start_transform = ref.transform
    from PySide6.QtCore import QPointF
    sx, _ = item._resize_snap(Handle.R, QPointF(0, 40), 2.84, 1.0)
    assert sx * 100.0 == pytest.approx(287.0)


def test_resize_snap_corner_snaps_both_axes(qapp):
    """右下角：x 吸附纸右缘、y 吸附页边距下线，两轴同时生效。"""
    ctrl = DocumentController(Document())
    ref, item = _page_ref_item(ctrl)
    item._start_transform = ref.transform
    from PySide6.QtCore import QPointF
    sx, sy = item._resize_snap(Handle.BR, QPointF(0, 80), 2.94, 0.875)
    assert sx * 100.0 == pytest.approx(297.0)
    assert 80.0 - sy * 80.0 == pytest.approx(10.0)   # 手柄落在 y=10 内框线


def test_resize_snap_rotated_reference(qapp):
    """旋转过的参考图同样能吸附（按页面空间位移方向反解缩放系数）。

    参考图旋转 90° 后，右缘手柄在页面里是沿 +Y 移动的——应吸附到纸张
    上边缘（A4 高 210），而不是按手柄的本地轴去找 x 目标。
    """
    ctrl = DocumentController(Document())
    ref, item = _page_ref_item(ctrl)
    ref.transform = AffineTransform.translate(50, 60) @ AffineTransform.rotate(90)
    item._start_transform = ref.transform
    pos = item._handle_positions()
    A = pos[Handle.L]
    P = pos[Handle.R]
    by = ref.transform.apply((A.x(), A.y()))[1]
    dy = ref.transform.apply((P.x(), A.y()))[1] - by
    sx, sy = item._resize_snap(Handle.R, A, (205.0 - by) / dy, 1.0)
    assert by + sx * dy == pytest.approx(210.0)   # 吸附到纸上边缘
    assert sy == 1.0


def test_resize_snap_disabled_or_far(qapp):
    """关闭开关、或离边缘很远时不吸附。"""
    ctrl = DocumentController(Document())
    ref, item = _page_ref_item(ctrl)
    item._start_transform = ref.transform
    from PySide6.QtCore import QPointF
    ref.snap_to_page = False
    assert item._resize_snap(Handle.R, QPointF(0, 40), 2.90, 1.0) == (2.90, 1.0)
    ref.snap_to_page = True
    assert item._resize_snap(Handle.R, QPointF(0, 40), 2.0, 1.0) == (2.0, 1.0)


def test_resize_snap_through_drag_path(qapp):
    """完整缩放链路：拖右缘到纸边附近，提交的变换右缘落在 297mm。"""
    ctrl = DocumentController(Document())
    ref, item = _page_ref_item(ctrl)
    item._start_transform = ref.transform
    from PySide6.QtCore import QPointF
    item._mode = Handle.R
    item._start_scene = QPointF(100, 40)
    item._start_local = QPointF(100, 40)
    item._pending = None
    ev = _FakeEvent((296.0, 40.0), (0, 0))       # 右缘 296 → 吸附 297
    t = item._compute_handle_transform(ev)
    x = t.apply((100.0, 40.0))[0]
    assert x == pytest.approx(297.0)


def test_resize_snap_skipped_with_shift(qapp, monkeypatch):
    """按住 Shift 等比缩放时不吸附，避免破坏比例。"""
    ctrl = DocumentController(Document())
    ref, item = _page_ref_item(ctrl)
    item._start_transform = ref.transform
    from PySide6.QtCore import QPointF
    calls = []
    monkeypatch.setattr(item, "_resize_snap",
                        lambda *a, **k: calls.append(a) or (a[2], a[3]))
    item._mode = Handle.BR
    item._start_local = QPointF(100, 0)
    item._start_scene = QPointF(100, 0)
    ev = _FakeEvent((300, 5), (0, 0), modifiers=Qt.ShiftModifier)
    item._compute_handle_transform(ev)
    assert calls == []


def test_reference_snap_to_page_serialization_roundtrip():
    ref = ReferenceItem(width_mm=10, height_mm=10)
    assert ref.snap_to_page is True                 # 新参考图默认开启
    ref.snap_to_page = False
    assert ReferenceItem.from_data(ref.to_data()).snap_to_page is False
    # 旧项目无此键 → 取默认（开启）
    assert ReferenceItem.from_data({"width_mm": 10}).snap_to_page is True


def test_reference_panel_snap_resize_toggle(qapp):
    ctrl = DocumentController(Document())
    ref = ReferenceItem(width_mm=50, height_mm=30)
    ctrl.add_reference(ref)
    from writerstudio.ui.reference_panel import ReferencePanel
    panel = ReferencePanel(ctrl)
    panel.select_ref(ref)
    assert panel.snap_resize_check.isChecked() is True
    panel.snap_resize_check.setChecked(False)
    assert ref.snap_to_page is False
    ctrl.undo()
    assert ref.snap_to_page is True


def test_reference_drag_disables_smooth_pixmap_while_dragging(qapp):
    """拖动参考图期间关闭位图平滑（快速预览），松手恢复。"""
    ctrl = DocumentController(Document())
    ref = ReferenceItem(width_mm=50, height_mm=30)
    ctrl.add_reference(ref)
    from writerstudio.ui.canvas import CanvasView
    view = CanvasView(ctrl)
    view.resize(600, 400)
    _keep_alive(view)
    item = view._ref_items[ref.id]
    from PySide6.QtGui import QPainter
    smooth = QPainter.SmoothPixmapTransform
    assert bool(view.renderHints() & smooth)
    item._begin_drag('move', None, _FakeEvent((10, 10), (10, 10)))
    assert not bool(view.renderHints() & smooth)
    item.mouseReleaseEvent(_FakeEvent((20, 10), (20, 10)))
    assert bool(view.renderHints() & smooth)


def test_canvas_pen_marker_set_and_drag_signal(qapp):
    doc = Document()
    ctrl = DocumentController(doc)
    from writerstudio.ui.canvas import CanvasView
    view = CanvasView(ctrl)
    view.resize(600, 400)
    view.fit_page()
    _keep_alive(view)
    view.set_pen_marker((10.0, 20.0))
    assert view.pen_marker() == (10.0, 20.0)

    got = []
    view.penMarkerMoved.connect(lambda x, y: got.append((x, y)))
    vp = view._marker_view_pos().toPoint()
    ev = _FakeMouseEvent(vp)
    view.mousePressEvent(ev)
    assert view._marker_dragging
    view.mouseMoveEvent(_FakeMouseEvent(vp + QPoint(12, 0)))
    view.mouseReleaseEvent(_FakeMouseEvent(vp + QPoint(12, 0)))
    assert got and got[-1][0] > 10.0


def test_reference_panel_visibility_toggle(qapp):
    doc = Document()
    ctrl = DocumentController(doc)
    ref = ReferenceItem(width_mm=50, height_mm=30)
    ctrl.add_reference(ref)
    from writerstudio.ui.reference_panel import ReferencePanel
    panel = ReferencePanel(ctrl)
    assert panel.list.count() == 1
    item = panel.list.item(0)
    item.setCheckState(Qt.Unchecked)   # 取消勾选 → 隐藏
    assert ref.visible is False


def test_main_window_has_reference_and_page_features(qapp):
    win = MainWindow(settings=_mem_settings())
    win.confirm_on_close = False
    assert hasattr(win, "reference_panel")
    assert hasattr(win, "page_presets")
    assert win.reference_panel.list.count() == 0
    win.close()


def test_main_window_start_marker_flow(qapp):
    win = MainWindow(settings=_mem_settings())
    win.confirm_on_close = False
    mp = win.machine_panel
    # 模拟画布拖动起点标记：页面坐标经固定映射存为机器坐标（y 翻转）
    win._on_pen_marker_moved(42.0, 24.0)
    marker_m = mp.start_point.pen_marker
    assert marker_m is not None
    assert mp.start_point.mode == "canvas"
    assert mp.start_point.point == pytest.approx(marker_m)
    page = win.controller.doc.page
    assert marker_m == pytest.approx((42.0, page.height - 24.0))
    # 固定映射下起点不再产生书写偏移
    assert win._machine_config().origin_offset == (0.0, 0.0)
    # 显示回页面坐标必须精确等于拖动位置（机器坐标 ↔ 页面坐标自逆）
    win._refresh_pen_marker_canvas()
    assert win.canvas.pen_marker() == pytest.approx((42.0, 24.0))
    win.close()


def test_marker_does_not_move_content(qapp):
    """固定映射：拖动标记只定停笔位，内容落点与标记无关（WYSIWYG）。"""
    win = MainWindow(settings=_mem_settings())
    win.confirm_on_close = False
    win.machine_panel
    from writerstudio.core.document import make_static_object
    win.controller.add_object(make_static_object([make_rect(0, 0, 20, 10)]))
    win.canvas.sync_scene()
    win._on_pen_marker_moved(200.0, 150.0)   # 页面坐标（画布拖动）
    win._refresh_pen_marker_canvas()
    assert win._machine_config().origin_offset == (0.0, 0.0)
    win.close()


def test_read_pen_as_start_from_fake_machine(qapp):
    """连接假串口后「读取当前笔位作为起点」应把起点设为笔头坐标。"""
    import time
    from writerstudio.machine.serial_link import FakeSerial
    win = MainWindow(settings=_mem_settings())
    win.confirm_on_close = False
    mp = win.machine_panel
    fake = FakeSerial(initial_pos=(60.0, 80.0, 0.0))
    mp.link.connect_backend(fake)

    win._on_read_pen_as_start()
    end = time.time() + 2.0
    while time.time() < end and mp.start_point.pen_marker is None:
        qapp.processEvents()
        time.sleep(0.005)
    # 读取到的笔头位置成为写字起点，模式切到「画布指定点」，标记同步
    assert mp.start_point.pen_marker == pytest.approx((60.0, 80.0))
    assert mp.start_point.mode == "canvas"
    assert mp.start_point.point == pytest.approx((60.0, 80.0))
    mp.link.disconnect()
    win.close()


def test_read_pen_as_start_without_machine_is_safe(qapp):
    """未连接机器时点「读取当前笔位作为起点」不崩溃、不弹模态框。"""
    win = MainWindow(settings=_mem_settings())
    win.confirm_on_close = False
    win._on_read_pen_as_start()
    assert win.machine_panel.start_point.pen_marker is None
    win.close()


def test_manual_mark_start_pick(qapp):
    """「手动标记起点」进入画布拾取，单击点成为起点并落标记。"""
    win = MainWindow(settings=_mem_settings())
    win.confirm_on_close = False
    mp = win.machine_panel
    mp.mark_start_btn.setChecked(True)          # 触发 markStartPickRequested
    assert win.canvas._pick_mode
    win._on_start_marked(50.0, 60.0)            # 模拟画布单击
    # 页面坐标经固定映射存为机器坐标：机器 y = 纸高 − 页面 y
    page = win.controller.doc.page
    assert mp.start_point.pen_marker == pytest.approx((50.0, page.height - 60.0))
    assert mp.start_point.mode == "canvas"
    assert mp.start_point.point == pytest.approx((50.0, page.height - 60.0))
    assert not mp.mark_start_btn.isChecked()    # 拾取完成按钮自动复位
    win.close()


def test_marker_reprojects_consistently_when_mode_changes(qapp):
    """起点模式变化后标记的页面位置保持不变（固定映射与模式无关）。"""
    win = MainWindow(settings=_mem_settings())
    win.confirm_on_close = False
    mp = win.machine_panel
    win._on_pen_marker_moved(100.0, 80.0)
    win._refresh_pen_marker_canvas()
    shown = win.canvas.pen_marker()
    assert shown == pytest.approx((100.0, 80.0))
    # 切换起点模式（标记不变，这里切到笔位校准记录一点）→ 显示位置仍一致
    mp.start_point.mode = "registered"
    mp.start_point.point = (5.0, 5.0)
    win._refresh_pen_marker_canvas()
    assert win.canvas.pen_marker() == pytest.approx((100.0, 80.0))
    # 机械原点标注恒在左上角
    page = win.controller.doc.page
    assert win.canvas.machine_origin()[0] == pytest.approx((0.0, page.height))
    win.close()


def test_origin_marker_pinned_top_left_regardless_of_start(qapp):
    """机械原点标注恒在左上角：改起点/拖标记（哪怕越界）都不动它。

    回归：旧版标注 = −起点偏移，起点被钳成 (500,500) 后标注被画到
    纸外约 500mm 处，看起来"消失"；拖动起点时标注坐标乱跳。
    """
    win = MainWindow(settings=_mem_settings())
    win.confirm_on_close = False
    mp = win.machine_panel
    page = win.controller.doc.page
    pos = win.canvas.machine_origin()[0]
    assert pos == pytest.approx((0.0, page.height))
    mp.set_start_point("canvas", 632.7, 611.0)
    win._on_pen_marker_moved(632.7, 611.0)
    assert win.canvas.machine_origin()[0] == pytest.approx((0.0, page.height))
    win.close()


def test_page_setup_applies_and_cancels(qapp, monkeypatch):
    """页面设置：确定时应用尺寸/边距/参考线并持久化；取消时不改任何东西。"""
    from writerstudio.ui import main_window as mwmod
    from PySide6.QtWidgets import QDialog

    backend = _MemoryBackend()
    win = MainWindow(settings=Settings(backend))
    win.confirm_on_close = False

    class Fake:
        Accepted = QDialog.Accepted

        def __init__(self, *a, **k):
            pass

        def exec(self):
            return QDialog.Accepted

        def result_width(self):
            return 297.0

        def result_height(self):
            return 420.0

        def result_margin(self):
            return 15.0

        def result_preset_name(self):
            return "A3"

        def guide_visible(self):
            return False

    monkeypatch.setattr(mwmod, "PageSetupDialog", Fake)
    win._page_setup()
    page = win.controller.doc.page
    assert (page.width, page.height, page.margin) == (297.0, 420.0, 15.0)
    assert page.preset_name == "A3"
    assert win.canvas.show_page_guide is False
    assert backend.value("page/width") == 297.0 and backend.value("page/preset_name") == "A3"

    class FakeCancel(Fake):
        def exec(self):
            return QDialog.Rejected

    monkeypatch.setattr(mwmod, "PageSetupDialog", FakeCancel)
    before = (page.width, page.height, page.margin, page.preset_name,
              win.canvas.show_page_guide)
    win._page_setup()
    assert (page.width, page.height, page.margin, page.preset_name,
            win.canvas.show_page_guide) == before
    win.close()


def test_locked_reference_cannot_be_dragged(qapp):
    """锁定的参考图不应被拖动，也不产生撤销项。"""
    ctrl = DocumentController(Document())
    ref = ReferenceItem(width_mm=100, height_mm=50, locked=True)
    ctrl.add_reference(ref)
    from writerstudio.ui.canvas import CanvasView
    view = CanvasView(ctrl)
    view.resize(600, 400)
    view.fit_page()
    _keep_alive(view)
    item = view._ref_items[ref.id]
    t0 = ref.transform
    item._begin_drag('move', None, _FakeEvent((0, 0), (0, 0)))
    assert item._drag_kind is None            # 被锁定拦截
    n0 = ctrl.undo_stack.count()
    item._commit_transform(AffineTransform.translate(9, 9) @ t0, "移动")
    assert ref.transform == t0                # 不写回
    assert ctrl.undo_stack.count() == n0      # 不新增撤销项


def test_noop_transform_does_not_push_undo(qapp):
    ctrl = DocumentController(Document())
    ref = ReferenceItem(width_mm=10, height_mm=10)
    ctrl.add_reference(ref)
    n1 = ctrl.undo_stack.count()
    from writerstudio.ui.canvas import CanvasView
    view = CanvasView(ctrl)
    item = view._ref_items[ref.id]
    item._commit_transform(ref.transform, "移动")   # 零变化
    assert ctrl.undo_stack.count() == n1


def test_page_setup_dialog_preset_logic(qapp):
    store = PagePresetStore(Settings(_MemoryBackend()))
    page = PageSpec(297.0, 210.0, 10.0)
    dlg = PageSetupDialog(page, store, guide_visible=True)
    # 选 A4（横向为默认方向）
    idx = dlg.preset_combo.findData("A4")
    dlg.preset_combo.setCurrentIndex(idx)
    assert dlg.result_width() == pytest.approx(297.0)
    assert dlg.result_height() == pytest.approx(210.0)
    assert dlg.result_preset_name() == "A4"
    # 切纵向
    dlg.orient_combo.setCurrentIndex(1)
    assert dlg.result_width() == pytest.approx(210.0)
    assert dlg.result_height() == pytest.approx(297.0)
    # 关闭参考线
    dlg.guide_check.setChecked(False)
    assert dlg.guide_visible() is False


def test_page_preset_editor_dialog(qapp):
    store = PagePresetStore(Settings(_MemoryBackend()))
    dlg = PagePresetEditorDialog(store, PagePreset("新纸", 111.0, 222.0, 5.0))
    assert dlg.result_preset().name == "新纸"
    assert dlg.result_preset().width == 111.0


def test_main_window_page_setup_applies(qapp):
    win = MainWindow(settings=_mem_settings())
    win.confirm_on_close = False
    page = win.controller.doc.page
    # 直接模拟对话框结果路径：改用 store 里 A4 的尺寸
    preset = win.page_presets.find("A4")
    page.width, page.height = preset.oriented(True)
    page.preset_name = "A4"
    win.canvas._update_scene_rect()
    assert page.width == pytest.approx(297.0)
    assert page.height == pytest.approx(210.0)
    win.close()


# ---------------------------------------------------------------- 测试替身
_ALIVE: list = []


def _keep_alive(obj):
    _ALIVE.append(obj)


class _FakeEvent:
    def __init__(self, scene, local, modifiers=0):
        from PySide6.QtCore import QPointF
        self._scene = QPointF(*scene)
        self._local = QPointF(*local)
        self._mod = modifiers

    def scenePos(self):
        return self._scene

    def pos(self):
        return self._local

    def modifiers(self):
        return self._mod

    def accept(self):
        pass


class _FakeMouseEvent:
    """仿真 QMouseEvent 的 position()/button()，驱动画布笔位拖动。"""

    def __init__(self, pos):
        from PySide6.QtCore import QPointF, Qt
        self._pos = QPointF(pos)
        self._button = Qt.LeftButton
        self.accepted = False

    def position(self):
        return self._pos

    def button(self):
        return self._button

    def buttons(self):
        from PySide6.QtCore import Qt
        return Qt.LeftButton

    def modifiers(self):
        from PySide6.QtCore import Qt
        return Qt.NoModifier

    def accept(self):
        self.accepted = True

    def ignore(self):
        pass


# ==================================================== 起点与笔位组合并 / 面板宽度
def test_machine_panel_start_buttons(qapp):
    """写字起点组只保留三个按钮，无 emoji、无括号说明、无灰字引导。"""
    from PySide6.QtWidgets import QGroupBox, QScrollArea

    from writerstudio.ui.machine_panel import MachinePanel

    mp = MachinePanel()
    group = mp.read_pos_start_btn.parentWidget()
    while group is not None and not isinstance(group, QGroupBox):
        group = group.parentWidget()
    assert group is not None
    assert group.title() == "写字起点"
    texts = [mp.read_pos_start_btn.text(), mp.mark_start_btn.text(),
             mp.move_pen_btn.text()]
    assert texts == ["读取当前笔位作为起点", "手动标记起点", "移动到标记起点"]
    for t in texts:
        assert "(" not in t and "（" not in t and "📌" not in t
    assert mp.mark_start_btn.isCheckable()
    # 旧的多余控件已删除
    for gone in ("use_pen_pos_btn", "marker_check", "marker_label",
                 "read_pos_btn", "marker_to_start_btn", "start_hint"):
        assert not hasattr(mp, gone)
    # 最宽行拆行后面板内容的最小宽度应装进默认 300px 的 dock
    body = mp.findChild(QScrollArea).widget()
    assert body.minimumSizeHint().width() <= 300


def test_main_window_applies_default_dock_width(qapp):
    """默认布局下切到「机器控制」应完整显示面板（无横向滚动条）。"""
    from PySide6.QtWidgets import QScrollArea

    win = MainWindow(settings=_mem_settings())
    win.confirm_on_close = False
    win.show()
    qapp.processEvents()
    win._apply_default_dock_widths()
    win.machine_dock.raise_()   # 标签组的宽度要看可见的那个 tab
    qapp.processEvents()
    qapp.processEvents()
    assert win.machine_dock.width() >= 280
    sa = win.machine_panel.findChild(QScrollArea)
    assert not sa.horizontalScrollBar().isVisible()
    win.close()
