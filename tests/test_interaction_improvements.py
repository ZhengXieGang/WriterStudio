"""交互改进回归测试。

覆盖本轮修复/新增：
    * 全局面板与选中对象同步（不再误清扰动；无选择时静默不写）
    * 内容/扰动/页面编辑纳入撤销；连续拖动合并为一步
    * SourceSpec 深拷贝（复制对象不共享扰动）
    * 新建对象自动避让重叠
    * 写字起点统一为机器坐标（画布拾取自动换算）
    * 对象显示/锁定入口与锁定保护
    * 对齐/分布/缩放到选中
    * 缺字报告与越界检测
    * 参考层旋转与吸附
"""

from __future__ import annotations

import math
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from writerstudio.core.document import (  # noqa: E402
    ROTATE_180,
    ROTATE_CCW,
    ROTATE_CW,
    PageSpec,
    make_static_object,
    page_rotation,
)
from writerstudio.core.geometry import AffineTransform  # noqa: E402
from writerstudio.core.sample import make_rect  # noqa: E402
from writerstudio.fonts.builder import TextSpec, make_text_object  # noqa: E402
from writerstudio.machine.start_point import MODE_CANVAS  # noqa: E402
from writerstudio.perturb.params import PerturbParams  # noqa: E402
from writerstudio.settings import Settings, _MemoryBackend  # noqa: E402

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from writerstudio.ui.controller import (  # noqa: E402
    restore_object,
    snapshot_object,
)
from writerstudio.ui import main_window as _mw  # noqa: E402
from writerstudio.ui.main_window import MainWindow  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


def _mem() -> Settings:
    return Settings(_MemoryBackend())


def _win(qapp) -> MainWindow:
    w = MainWindow(settings=_mem())
    w.confirm_on_close = False
    return w


def _make_win(qapp, n_rects: int = 0):
    w = _win(qapp)
    w.controller.doc.objects.clear()
    for i in range(n_rects):
        w.controller.doc.objects.append(
            make_static_object([make_rect(0, 0, 20, 10)], name=f"r{i}",
                               transform=AffineTransform.translate(10 + 40 * i, 10)))
    w.canvas.sync_scene()
    return w


# ===================================================== 深拷贝
def test_source_spec_clone_is_deep():
    obj = make_text_object(TextSpec(text="A", font_names=["futural"],
                                    perturb=PerturbParams.natural(10.0)),
                           __import__("writerstudio.fonts.manager",
                                      fromlist=["FontManager"]).FontManager())
    clone = obj.clone()
    clone.source.data["perturb"]["char_x_sigma"] = 99.0
    assert obj.source.data["perturb"]["char_x_sigma"] != 99.0


def test_object_snapshot_restore_roundtrip(qapp):
    w = _win(qapp)
    obj = make_text_object(TextSpec(text="A", font_names=["futural"]),
                           w.font_manager)
    snap = snapshot_object(obj)
    orig_text = obj.source.data["text"]
    obj.source.data["text"] = "changed"
    obj.name = "改了"
    restore_object(obj, snap)
    assert obj.source.data["text"] == orig_text and obj.name != "改了"


# ===================================================== 全局面板同步
def test_global_panel_loads_selection_params(qapp):
    w = _win(qapp)
    obj = make_text_object(TextSpec(text="Hello", font_names=["futural"],
                                    perturb=PerturbParams.natural(10.0)),
                           w.font_manager)
    w.controller.add_object(obj)
    w.canvas.sync_scene()
    w.canvas.select_object(obj)
    w.controller.selectionChanged.emit()
    p = w.global_perturb.params()
    assert p.enabled is True
    assert p.char_x_sigma == pytest.approx(
        obj.source.data["perturb"]["char_x_sigma"])
    assert w.global_perturb.preset_combo.currentText() == "自然"
    w.close()


def test_global_panel_slider_keeps_perturb_and_undoable(qapp):
    w = _win(qapp)
    obj = make_text_object(TextSpec(text="Hello", font_names=["futural"],
                                    perturb=PerturbParams.natural(10.0)),
                           w.font_manager)
    w.controller.add_object(obj)
    w.canvas.sync_scene()
    w.canvas.select_object(obj)
    w.controller.selectionChanged.emit()
    before = obj.source.data["perturb"]["char_x_sigma"]
    n0 = w.controller.undo_stack.count()
    # 只动一个滑杆：不得清除 enabled，可撤销（防抖后 flush 统一应用）
    w.global_perturb._spin["char_x_sigma"].setValue(before + 0.1)
    w.global_perturb.flush()
    assert obj.source.data["perturb"]["enabled"] is True
    assert obj.source.data["perturb"]["char_x_sigma"] != before
    assert w.controller.undo_stack.count() == n0 + 1
    w.controller.undo()
    assert obj.source.data["perturb"]["char_x_sigma"] == pytest.approx(before)
    w.close()


def test_global_panel_slider_without_selection_is_noop(qapp):
    w = _win(qapp)
    w.canvas.clear_selection()
    n0 = w.controller.undo_stack.count()
    w.global_perturb._spin["char_x_sigma"].setValue(1.23)
    w.global_perturb.flush()
    assert w.controller.undo_stack.count() == n0     # 不产生撤销项
    assert "选择" in w.statusBar().currentMessage()
    w.close()


def test_global_panel_drag_merges_into_one_undo(qapp):
    w = _win(qapp)
    obj = make_text_object(TextSpec(text="Hi", font_names=["futural"],
                                    perturb=PerturbParams.natural(10.0)),
                           w.font_manager)
    w.controller.add_object(obj)
    w.canvas.sync_scene()
    w.canvas.select_object(obj)
    w.controller.selectionChanged.emit()
    n0 = w.controller.undo_stack.count()
    base = obj.source.data["perturb"]["char_y_sigma"]
    for dv in (0.1, 0.2, 0.3, 0.4):
        w.global_perturb._spin["char_y_sigma"].setValue(base + dv)
    w.global_perturb.flush()      # 连续调整防抖合并，落盘一次
    # 连续同键操作合并为一条
    assert w.controller.undo_stack.count() == n0 + 1
    w.close()


# ===================================================== 内容/扰动/页面撤销
def test_edit_text_is_undoable(qapp, monkeypatch):
    w = _win(qapp)
    obj = make_text_object(TextSpec(text="Hello", font_names=["futural"]),
                           w.font_manager)
    w.controller.add_object(obj)
    w.canvas.sync_scene()
    n0 = w.controller.undo_stack.count()
    original_strokes = len(obj.local_strokes)
    assert original_strokes > 0

    class FakeDlg:
        Accepted = 1

        def __init__(self, *a, **k):
            pass

        def exec(self):
            return 1

        def result_spec(self):
            return TextSpec(text="Changed", font_names=["futural"])

    monkeypatch.setattr(_mw, "TextEditDialog", FakeDlg)
    w._edit_object(obj)
    assert obj.source.data["text"] == "Changed"
    assert len(obj.local_strokes) != original_strokes
    assert w.controller.undo_stack.count() == n0 + 1
    w.controller.undo()
    assert obj.source.data["text"] == "Hello"
    assert len(obj.local_strokes) == original_strokes       # 笔画同步恢复
    w.controller.redo()
    assert obj.source.data["text"] == "Changed"
    w.close()


def test_edit_content_is_undoable(qapp, monkeypatch):
    w = _win(qapp)
    from writerstudio.content.builder import SOURCE_MARKDOWN
    obj = make_text_object(TextSpec(text="x", font_names=["futural"]),
                           w.font_manager)
    obj.source.kind = SOURCE_MARKDOWN
    obj.source.data = {"text": "# 旧标题", "font_names": [], "style": {},
                       "perturb": {}}
    w.controller.add_object(obj)
    w.canvas.sync_scene()

    class Payload:
        kind = "markdown"
        data = {"text": "# 新标题", "size": 4.0}

    class FakeCD:
        Accepted = 1

        def __init__(self, *a, **k):
            pass

        def exec(self):
            return 1

        def load_from_object(self, o):
            pass

        def payload(self):
            return Payload()

    monkeypatch.setattr(_mw, "ContentDialog", FakeCD)
    w._edit_content(obj)
    assert obj.source.data["text"] == "# 新标题"
    w.controller.undo()
    assert obj.source.data["text"] == "# 旧标题"
    w.close()


def test_clear_perturb_is_undoable(qapp):
    w = _win(qapp)
    obj = make_text_object(TextSpec(text="P", font_names=["futural"],
                                    perturb=PerturbParams.natural(10.0)),
                           w.font_manager)
    w.controller.add_object(obj)
    w.canvas.sync_scene()
    w.canvas.select_object(obj)
    w._clear_perturb()
    assert obj.source.data["perturb"]["enabled"] is False
    w.controller.undo()
    assert obj.source.data["perturb"]["enabled"] is True
    w.close()


def test_reseed_is_undoable(qapp):
    w = _win(qapp)
    obj = make_text_object(TextSpec(text="P", font_names=["futural"],
                                    perturb=PerturbParams.natural(10.0, seed=7)),
                           w.font_manager)
    w.controller.add_object(obj)
    w.canvas.sync_scene()
    w.canvas.select_object(obj)
    seed0 = obj.source.data["perturb"]["seed"]
    w._reseed_selected()
    assert obj.source.data["perturb"]["seed"] != seed0
    w.controller.undo()
    assert obj.source.data["perturb"]["seed"] == seed0
    w.close()


def test_page_command_undo(qapp):
    w = _win(qapp)
    old = PageSpec(297.0, 210.0, 10.0, "A4")
    new = PageSpec(210.0, 297.0, 15.0, "A4p")
    w.controller.set_page(old, new)
    assert (w.controller.doc.page.width, w.controller.doc.page.height) == (210.0, 297.0)
    assert w.controller.doc.page.margin == 15.0
    w.controller.undo()
    assert (w.controller.doc.page.width, w.controller.doc.page.height) == (297.0, 210.0)
    assert w.controller.doc.page.margin == 10.0
    w.close()


# ===================================================== 摆放避让
def test_place_object_avoids_overlap(qapp):
    w = _make_win(qapp)
    o1 = make_text_object(TextSpec(text="A", font_names=["futural"]),
                          w.font_manager)
    w._place_object(o1)
    w.controller.add_object(o1)
    o2 = make_text_object(TextSpec(text="B", font_names=["futural"]),
                          w.font_manager)
    w._place_object(o2)
    assert o1.transform != o2.transform
    b1, b2 = o1.world_bbox(), o2.world_bbox()
    overlap = (b1.x0 < b2.x1 and b1.x1 > b2.x0
               and b1.y0 < b2.y1 and b1.y1 > b2.y0)
    assert not overlap
    w.close()


# ===================================================== 起点坐标系统一
def test_start_pick_converts_page_to_machine(qapp):
    w = _win(qapp)
    # 页面→机器的拾取换算走固定映射（机械原点=纸张左上角，y 翻转）
    page = w.controller.doc.page
    expect = (100.0, page.height - 80.0)
    w._on_start_marked(100.0, 80.0)
    got = w.machine_panel.start_point.pen_marker
    assert got == pytest.approx(expect)
    w.close()


# ===================================================== 显示/锁定
def test_toggle_visible_and_lock(qapp):
    w = _make_win(qapp, 2)
    objs = list(w.controller.doc.objects)
    w.canvas.select_object(objs[0])
    w._toggle_visible_selected()
    assert objs[0].visible is False
    w.controller.undo()
    assert objs[0].visible is True

    w.canvas.select_object(objs[0])
    w._toggle_lock_selected()
    assert objs[0].locked is True
    w.controller.undo()
    assert objs[0].locked is False
    # 面板列表应标记状态
    objs[0].locked = True
    w._refresh_object_panel()
    assert "锁定" in w.obj_list.item(0).text()
    w.close()


def test_locked_object_not_draggable(qapp):
    w = _make_win(qapp, 1)
    obj = w.controller.doc.objects[0]
    obj.locked = True
    w.canvas.sync_scene()
    item = w.canvas._items[obj.id]
    t0 = obj.transform
    item._begin_drag("move", None, _FakeEv())
    assert item._drag_kind is None
    item._commit_transform(AffineTransform.translate(5, 5) @ t0, "移动")
    assert obj.transform == t0
    w.close()


def test_hidden_object_excluded_from_gcode(qapp):
    w = _make_win(qapp, 1)
    obj = w.controller.doc.objects[0]
    w.canvas.select_object(obj)
    w._toggle_visible_selected()
    from writerstudio.machine.gcode_gen import generate_from_document
    from writerstudio.machine.config import GCodeConfig
    r = generate_from_document(w.controller.doc, GCodeConfig())
    assert r.stroke_count == 0
    w.close()


# ===================================================== 对齐 / 分布 / 缩放
def test_align_left_and_undo(qapp):
    w = _make_win(qapp, 3)
    objs = list(w.controller.doc.objects)
    w.canvas.select_object(objs[0])
    for o in objs[1:]:
        w.canvas._items[o.id].setSelected(True)
    w._align("left")
    xs = [round(o.world_bbox().x0, 3) for o in objs]
    assert len(set(xs)) == 1
    w.controller.undo()
    assert len({round(o.world_bbox().x0, 3) for o in objs}) == 3
    w.close()


def test_distribute_horizontal_even(qapp):
    w = _make_win(qapp, 3)
    objs = list(w.controller.doc.objects)
    for o in objs:
        w.canvas._items[o.id].setSelected(True)
    w._distribute("h")
    cs = sorted(o.world_bbox().center[0] for o in objs)
    assert (cs[1] - cs[0]) == pytest.approx(cs[2] - cs[1], abs=1e-6)
    w.close()


def test_align_needs_two_objects(qapp):
    w = _make_win(qapp, 1)
    w.canvas.select_object(w.controller.doc.objects[0])
    w._align("left")   # 不应报错，只提示
    assert "至少两个" in w.statusBar().currentMessage()
    w.close()


def test_zoom_to_selection(qapp):
    w = _make_win(qapp, 1)
    w.canvas.resize(600, 400)
    w.canvas.fit_page()
    z0 = w.canvas.current_zoom()
    w.canvas.select_object(w.controller.doc.objects[0])
    assert w.canvas.zoom_to_selection() is True
    assert w.canvas.current_zoom() != pytest.approx(z0, rel=0.01)
    w.close()


# ===================================================== 缺字 / 越界
def test_missing_glyphs_reported(qapp):
    w = _win(qapp)
    obj = make_text_object(TextSpec(text="中文ABC", font_names=["futural"]),
                           w.font_manager)
    w.controller.add_object(obj)
    w.canvas.sync_scene()
    lay = obj.meta.get("layout")
    assert lay is not None and "中" in lay.missing and "文" in lay.missing
    assert "A" not in lay.missing          # 英文可绘
    w.canvas.select_object(obj)
    w._report_missing(obj)
    assert "缺字" in w.statusBar().currentMessage()
    w.close()


def test_bounds_detection(qapp):
    w = _win(qapp)
    w.controller.doc.objects.clear()
    bad = make_static_object([make_rect(0, 0, 50, 50)], name="越界",
                             transform=AffineTransform.translate(-10, 190))
    ok = make_static_object([make_rect(0, 0, 20, 20)], name="正常",
                            transform=AffineTransform.translate(50, 50))
    w.controller.doc.objects.extend([bad, ok])
    out = {o.name for o in w._check_bounds()}
    assert out == {"越界"}
    bad.visible = False
    assert {o.name for o in w._check_bounds()} == set()
    w.close()


def test_warn_bounds_nonblocking_when_hidden(qapp):
    w = _win(qapp)
    w.controller.doc.objects.clear()
    w.controller.doc.objects.append(
        make_static_object([make_rect(0, 0, 50, 50)], name="越界",
                           transform=AffineTransform.translate(-10, 190)))
    # 未显示窗口 → 不弹模态框，返回 True
    assert w._warn_bounds() is True
    w.close()


# ===================================================== 参考层旋转/吸附
def test_reference_rotation_and_snap(qapp, tmp_path):
    from PySide6.QtGui import QColor, QImage
    from writerstudio.ui.reference_panel import current_angle
    w = _win(qapp)
    img = QImage(300, 150, QImage.Format_ARGB32)
    img.fill(QColor("white"))
    p = tmp_path / "r.png"
    img.save(str(p))
    w._add_reference(str(p), "image")
    ref = w.controller.doc.references[0]
    w.reference_panel.list.setCurrentRow(0)
    w.reference_panel.angle_spin.setValue(30.0)
    assert current_angle(ref.transform) == pytest.approx(30.0, abs=0.01)
    # 旋转中心保持（绕自身中心）
    w.reference_panel._snap("center")
    pb = w.controller.doc.page.bbox()
    got = ref.world_bbox().center
    assert got[0] == pytest.approx(pb.center[0], abs=0.1)
    assert got[1] == pytest.approx(pb.center[1], abs=0.1)
    # 撤销吸附
    w.controller.undo()
    w.close()


# ===================================================== 旋转纸张
def test_page_rotation_maps_page_onto_new_sheet():
    """旋转后整张纸应恰好落在新页面范围内（不越界、不留空）。"""
    for direction, size in ((ROTATE_CW, (210.0, 297.0)),
                            (ROTATE_CCW, (210.0, 297.0)),
                            (ROTATE_180, (297.0, 210.0))):
        t, w, h = page_rotation(direction, 297.0, 210.0)
        assert (w, h) == size
        corners = [(0, 0), (297, 0), (297, 210), (0, 210)]
        out = [t.apply(p) for p in corners]
        xs = [p[0] for p in out]
        ys = [p[1] for p in out]
        assert min(xs) == pytest.approx(0.0) and max(xs) == pytest.approx(w)
        assert min(ys) == pytest.approx(0.0) and max(ys) == pytest.approx(h)
        assert t.determinant() == pytest.approx(1.0)   # 无镜像/缩放


def test_pagespec_rotated():
    p = PageSpec(210.0, 297.0, 12.0, "A4")
    cw = p.rotated(ROTATE_CW)
    assert (cw.width, cw.height) == (297.0, 210.0)
    assert cw.margin == 12.0 and cw.preset_name == "A4"
    assert (p.rotated(ROTATE_180).width, p.rotated(ROTATE_180).height) == (210.0, 297.0)


def test_rotate_page_rotates_content_and_reference(qapp):
    from pathlib import Path
    tpl = Path(__file__).resolve().parent.parent / "templates" / \
        "实验报告_A4_对齐模板.svg"
    if not tpl.exists():
        pytest.skip("模板文件不存在")
    w = _win(qapp)
    w.controller.doc.objects.clear()
    page = w.controller.doc.page
    page.width, page.height = 210.0, 297.0
    w.canvas._update_scene_rect()
    w.canvas.fit_page()
    obj = make_text_object(TextSpec(text="Hello", font_names=["futural"]),
                           w.font_manager)
    w._place_object(obj)
    w.controller.add_object(obj)
    w._add_reference(str(tpl), "svg")
    ref = w.controller.doc.references[0]
    w.reference_panel.list.setCurrentRow(0)
    w.reference_panel._place_natural()
    obj_before = obj.world_bbox().as_tuple()
    n_before = w.controller.undo_stack.count()

    w._rotate_page(ROTATE_CW)
    assert (page.width, page.height) == (297.0, 210.0)
    # 参考图随之旋转并恰好铺满新页面
    assert ref.world_bbox().as_tuple() == pytest.approx((0.0, 0.0, 297.0, 210.0))
    assert obj.world_bbox().as_tuple() != pytest.approx(obj_before)
    # 整组旋转是「一条」撤销记录
    assert w.controller.undo_stack.count() == n_before + 1

    w.controller.undo()
    assert (page.width, page.height) == (210.0, 297.0)
    assert obj.world_bbox().as_tuple() == pytest.approx(obj_before)
    assert ref.world_bbox().as_tuple() == pytest.approx((0.0, 0.0, 210.0, 297.0))
    w.close()


def test_rotate_cw_then_ccw_is_identity(qapp):
    w = _win(qapp)
    w.controller.doc.objects.clear()
    page = w.controller.doc.page
    page.width, page.height = 210.0, 297.0
    w.canvas._update_scene_rect()
    obj = make_static_object([make_rect(0, 0, 30, 20)], name="r",
                             transform=AffineTransform.translate(20, 30))
    w.controller.doc.objects.append(obj)
    before = obj.world_bbox().as_tuple()
    w._rotate_page(ROTATE_CW)
    w._rotate_page(ROTATE_CCW)
    assert (page.width, page.height) == (210.0, 297.0)
    b = obj.world_bbox().as_tuple()
    # 旋转两次回到原位（允许极小浮点误差）
    assert b == pytest.approx(before, abs=1e-6)
    w.close()


def test_page_setup_dialog_rotate_callback(qapp):
    """页面设置对话框内的旋转按钮应回调并刷新尺寸控件。"""
    from writerstudio.ui.page_dialog import PageSetupDialog
    from writerstudio.page_presets import PagePresetStore
    page = PageSpec(210.0, 297.0, 10.0)
    calls = []

    def on_rotate(d):
        calls.append(d)
        # 模拟主窗口旋转：交换宽高
        page.width, page.height = page.height, page.width

    dlg = PageSetupDialog(page, PagePresetStore(_mem()), on_rotate=on_rotate)
    dlg._rotate(ROTATE_CW)
    assert calls == [ROTATE_CW]
    assert dlg.result_width() == pytest.approx(297.0)
    assert dlg.result_height() == pytest.approx(210.0)


# ===================================================== 拖拽稳定性（回归）
class _RealEv:
    """模拟真实 Qt 鼠标事件。

    关键：``QGraphicsSceneMouseEvent.pos()`` 是「按 item 当前变换反算出的本地
    坐标」，会随 item 变换改变。旧实现用它算缩放比例，等价于把一个随变换变化
    的量再喂回变换，形成反馈回路 → 对象拖拽时抖动/弹回。这里如实复现该行为。
    """

    def __init__(self, item, x: float, y: float):
        from PySide6.QtCore import QPointF
        self._item = item
        self._p = QPointF(x, y)

    def scenePos(self):
        return self._p

    def pos(self):
        inv, ok = self._item.transform().inverted()
        return inv.map(self._p) if ok else self._p

    def modifiers(self):
        return 0

    def accept(self):
        pass


def _scene_box(item):
    """item 本地包围盒经当前变换后的 scene 包围盒（宽, 高）。"""
    poly = item.transform().mapRect(item._local_bbox())
    return poly


def test_scale_drag_does_not_feedback_oscillate(qapp):
    """缩放拖拽必须单调、稳定：同一屏幕位置反复求值结果不得漂移。"""
    from writerstudio.ui.items import Handle
    w = _make_win(qapp, 1)                 # rect 20×10，变换 translate(10,10)
    obj = w.controller.doc.objects[0]
    w.canvas.select_object(obj)
    item = w.canvas._items[obj.id]

    # BR 手柄：本地 (20,0) → scene (30,10)
    item._begin_drag("handle", Handle.BR, _RealEv(item, 30.0, 10.0))
    assert item._start_local.x() == pytest.approx(20.0)
    assert item._start_local.y() == pytest.approx(0.0)

    widths = []
    for sx in range(31, 101):
        t = item._compute_handle_transform(_RealEv(item, float(sx), 10.0))
        item._preview(t)
        widths.append(_scene_box(item).width())

    # 单调不回弹（旧实现会出现来回跳动）
    assert all(b >= a - 1e-6 for a, b in zip(widths, widths[1:])), widths
    # 终点：右边缘拖到 scene x=100，宽度应约为 90
    assert widths[-1] == pytest.approx(90.0, rel=1e-3)

    # 鼠标停在同一点再发事件（真实设备每次移动都会发）不得改变变换
    t_a = item._compute_handle_transform(_RealEv(item, 100.0, 10.0))
    t_b = item._compute_handle_transform(_RealEv(item, 100.0, 10.0))
    assert t_a == t_b
    w.close()


def test_rotate_drag_does_not_feedback_oscillate(qapp):
    """旋转拖拽同样不能因 event.pos() 随变换改变而抖动。"""
    from writerstudio.ui.items import Handle
    w = _make_win(qapp, 1)
    obj = w.controller.doc.objects[0]
    w.canvas.select_object(obj)
    item = w.canvas._items[obj.id]

    box = item._local_bbox()
    center = box.center()
    rp = item._handle_positions()[Handle.ROTATE]
    start_scene = item.transform().map(rp)
    item._begin_drag("handle", Handle.ROTATE, _RealEv(item, start_scene.x(),
                                                     start_scene.y()))

    angles = []
    for step in range(1, 21):          # 8°×20 = 160°，不跨 180° 以免 atan2 回绕
        # 绕对象中心在 scene 上画弧（用起始变换映射本地旋转，避免依赖实现）
        local = AffineTransform.rotate(8.0 * step).apply(
            (rp.x() - center.x(), rp.y() - center.y()))
        scene = item._start_transform.apply((center.x() + local[0],
                                             center.y() + local[1]))
        t = item._compute_handle_transform(_RealEv(item, scene[0], scene[1]))
        item._preview(t)
        angles.append(math.degrees(math.atan2(t.b, t.a)))

    assert all(b >= a - 1e-6 for a, b in zip(angles, angles[1:])), angles
    assert angles[-1] == pytest.approx(160.0, abs=0.5)
    w.close()


def test_rotate_after_nonuniform_scale_keeps_aspect(qapp):
    """回归：非等比缩放后再旋转，不得产生剪切把宽高比拉伸。

    旧实现把旋转写成本地空间的「先转后乘 start_transform」，对象被非等比缩放后
    旋转，等价于「旋转 + 非等比缩放」（两者不可交换）→ 剪切形变。
    """
    from writerstudio.ui.items import Handle
    w = _make_win(qapp, 1)                    # 20×10 矩形
    obj = w.controller.doc.objects[0]
    w.canvas.select_object(obj)
    item = w.canvas._items[obj.id]

    # 先做非等比缩放（宽 2×、高 1×）→ 世界包围盒 40×10
    obj.transform = AffineTransform.scale(2.0, 1.0) @ obj.transform
    item.sync()
    box0 = _scene_box(item)
    assert box0.width() == pytest.approx(40.0)
    assert box0.height() == pytest.approx(10.0)

    # 再绕对象中心旋转 90°（用真实拖拽路径）
    box = item._local_bbox()
    _c = item.transform().map(box.center())
    cx, cy = _c.x(), _c.y()
    rp = item.transform().map(item._handle_positions()[Handle.ROTATE])
    item._begin_drag("handle", Handle.ROTATE, _RealEv(item, rp.x(), rp.y()))
    # 目标：把旋转柄从当前方向转到 +90°
    import math as _m
    r = _m.hypot(rp.x() - cx, rp.y() - cy)
    a0 = _m.atan2(rp.y() - cy, rp.x() - cx)
    tgt = (cx + r * _m.cos(a0 + _m.pi / 2), cy + r * _m.sin(a0 + _m.pi / 2))
    t = item._compute_handle_transform(_RealEv(item, tgt[0], tgt[1]))
    obj.transform = t
    item.sync()

    box1 = _scene_box(item)
    # 旋转 90° 后应仍是 40×10 的矩形（只是长宽方向对调），绝不是被剪切拉伸
    assert box1.width() == pytest.approx(10.0, abs=0.5)
    assert box1.height() == pytest.approx(40.0, abs=0.5)
    # 线性部分为 R·S（正交矩阵 × 对角缩放）：两列必须互相垂直 → 无剪切
    a, b, c, d = t.a, t.b, t.c, t.d
    assert a * c + b * d == pytest.approx(0.0, abs=1e-9)
    # 两列正交时，奇异值即两列长度；旋转不改变缩放量，故仍为 1 与 2
    col_norms = sorted([_m.hypot(a, b), _m.hypot(c, d)])
    assert col_norms == pytest.approx([1.0, 2.0], rel=1e-9)
    w.close()


def test_history_panel_is_compact_top_right_dock(qapp):
    """历史面板应停在右上角一小块，而不是与对象面板 tabify 占满右侧。"""
    from PySide6.QtCore import Qt
    w = _win(qapp)
    w.show()
    qapp.processEvents()
    assert w.dockWidgetArea(w.undo_panel) == Qt.RightDockWidgetArea
    # 历史独占一个标签组（不与对象/机器控制等合并）
    assert w.tabifiedDockWidgets(w.undo_panel) == []
    # 对象与机器控制仍共享一个标签组
    assert w.machine_dock in w.tabifiedDockWidgets(w.dock)
    # 历史位于对象面板**上方**（右上角），且高度受限、不占满整列
    assert w.undo_panel.geometry().top() < w.dock.geometry().top()
    assert w.undo_panel.maximumHeight() <= 300
    w.close()


# ===================================================== 辅助
class _FakeEv:
    def __init__(self, x: float = 0.0, y: float = 0.0):
        from PySide6.QtCore import QPointF
        self._p = QPointF(x, y)

    def scenePos(self):
        return self._p

    def pos(self):
        return self._p

    def modifiers(self):
        return 0

    def accept(self):
        pass


# ===================================================== UI 小问题修复
def test_view_menu_checkbox_tracks_panel_visibility(qapp):
    """用面板自身的关闭按钮隐藏后，视图菜单勾选应同步取消。"""
    w = _win(qapp)
    w.show()
    qapp.processEvents()
    assert w.act_show_machine.isChecked() is True
    w.machine_dock.setVisible(False)          # 模拟点面板标题栏的关闭
    qapp.processEvents()
    assert w.act_show_machine.isChecked() is False
    w.machine_dock.setVisible(True)
    qapp.processEvents()
    assert w.act_show_machine.isChecked() is True
    w.close()


def test_machine_panel_is_scrollable(qapp):
    """机器面板底部控件多，必须放在滚动区内才能操作到。"""
    from PySide6.QtWidgets import QScrollArea
    w = _win(qapp)
    areas = w.machine_panel.findChildren(QScrollArea)
    assert areas, "机器面板应包含 QScrollArea"
    assert areas[0].widgetResizable()
    w.close()


def test_use_pen_pos_as_start_sets_canvas_mode(qapp):
    """「读取当前笔位作为起点」应写入画布指定点模式的机器坐标。"""
    w = _win(qapp)
    # 无机器时不应崩溃（只给状态栏提示，不弹模态框）
    w._on_read_pen_as_start()
    assert w.machine_panel.start_point.pen_marker is None
    # 模拟已读到机器坐标
    w._read_machine_pos = lambda: (123.0, 45.0)
    w._on_read_pen_as_start()
    assert w.machine_panel.start_point.mode == MODE_CANVAS
    assert w.machine_panel.start_point.point == pytest.approx((123.0, 45.0))
    # 起点标记与起点重合，画布机械原点标注也已更新
    assert w.machine_panel.start_point.pen_marker == pytest.approx((123.0, 45.0))
    assert w.canvas.machine_origin() is not None
    w.close()


def test_start_point_summary_updates(qapp):
    w = _win(qapp)
    w.machine_panel.set_start_point(MODE_CANVAS, 12.0, 34.0)
    assert "12.0" in w.machine_panel.start_summary.text()
    w.close()


def test_canvas_middle_button_pans(qapp):
    """按住鼠标中键拖动应平移视图（而不是必须用滚动条）。"""
    from PySide6.QtCore import QEvent, QPoint, QPointF
    from PySide6.QtGui import QMouseEvent
    w = _win(qapp)
    w.resize(1000, 700)
    w.show()
    qapp.processEvents()
    c = w.canvas
    c.zoom_100()
    qapp.processEvents()

    def ev(t, p, btn=Qt.MiddleButton, btns=Qt.MiddleButton):
        return QMouseEvent(t, QPointF(p), QPointF(p), btn, btns, Qt.NoModifier)

    before = c.mapToScene(c.viewport().rect().center())
    c.mousePressEvent(ev(QEvent.MouseButtonPress, QPoint(400, 300)))
    c.mouseMoveEvent(ev(QEvent.MouseMove, QPoint(250, 180)))
    c.mouseReleaseEvent(ev(QEvent.MouseButtonRelease, QPoint(250, 180),
                           Qt.MiddleButton, Qt.NoButton))
    after = c.mapToScene(c.viewport().rect().center())
    assert (abs(before.x() - after.x()) > 1.0
            or abs(before.y() - after.y()) > 1.0)
    w.close()


def test_text_dialog_multifont_controls(qapp):
    """文本对话框应提供权重、随机、兜底字体，并能往返保存。"""
    from writerstudio.fonts.builder import TextSpec
    from writerstudio.ui.text_dialog import TextEditDialog
    w = _win(qapp)
    specs = ["futural", "scripts"]
    spec = TextSpec(text="AB", font_names=specs, size=10,
                    font_weights={specs[0]: 2.0, specs[1]: 1.0},
                    random_fonts=True, fallback_font="scripts", font_seed=42)
    dlg = TextEditDialog(spec, w.font_manager, w)
    dlg.chain_list.setCurrentRow(0)
    assert dlg.weight_spin.value() == pytest.approx(2.0)
    assert dlg.random_check.isChecked() is True
    out = dlg.result_spec()
    assert out.font_weights[specs[0]] == pytest.approx(2.0)
    assert out.random_fonts is True
    assert out.fallback_font == "scripts"
    assert out.font_seed == 42
    dlg.close()
    w.close()


def test_vector_object_perturb_via_main_window(qapp):
    """矢量对象走扰动菜单应能应用并可撤销。"""
    w = _make_win(qapp, 1)
    obj = w.controller.doc.objects[0]
    base = [s.points for s in obj.local_strokes]
    w.canvas.select_object(obj)
    from writerstudio.perturb.apply import apply_perturb
    from writerstudio.perturb.params import PerturbParams
    snapshot = snapshot_object(obj)
    apply_perturb(obj, PerturbParams.vector_hand_drawn(20.0), w.font_manager)
    w.controller.replace_object(obj, snapshot, snapshot_object(obj), "调整手写扰动")
    assert [s.points for s in obj.local_strokes] != base
    w.controller.undo()
    assert [s.points for s in obj.local_strokes] == base
    w.close()


def test_page_size_visible_and_clickable_in_status_bar(qapp):
    """纸张尺寸应常驻状态栏，并随页面设置更新。"""
    w = _win(qapp)
    txt = w._status_page.text()
    assert "mm" in txt
    from writerstudio.core.document import PageSpec
    old = w.controller.doc.page
    new = PageSpec(210.0, 297.0, old.margin, "A4")
    w.controller.set_page(PageSpec(old.width, old.height, old.margin,
                                   old.preset_name), new)
    w._update_page_status()
    assert "A4" in w._status_page.text() and "210" in w._status_page.text()
    w.close()


def test_page_setup_action_on_toolbar(qapp):
    w = _win(qapp)
    tbs = w.findChildren(__import__("PySide6.QtWidgets",
                                    fromlist=["QToolBar"]).QToolBar)
    acts = [a for tb in tbs for a in tb.actions()]
    assert w.act_page_setup in acts
    w.close()


def test_handles_stay_square_and_hittable_under_nonuniform_scale(qapp):
    """回归：对象被非等比缩放后，选择手柄必须仍是同样大小的方块（不被拉伸），
    且命中检测与画出的手柄一致（在屏幕空间比较）。"""
    from writerstudio.ui.items import Handle
    w = _make_win(qapp, 1)
    obj = w.controller.doc.objects[0]
    w.canvas.resize(900, 600)
    w.show()
    qapp.processEvents()
    item = w.canvas._items[obj.id]

    for sx, sy in [(6.0, 1.0), (1.0, 6.0), (5.0, 0.4)]:
        obj.transform = AffineTransform.scale(sx, sy)
        item.sync()
        item.setSelected(True)
        qapp.processEvents()

        # 屏幕空间 1px 对应的页面/本地单位
        scene_per_px = item._scene_per_px()
        at = item.model.transform          # 页面/本地坐标用 AffineTransform
        hbr = item._handle_positions()[Handle.BR]
        br_scene = at.apply((hbr.x(), hbr.y()))
        inv = at.invert()
        from PySide6.QtCore import QPointF
        # 正好落在 BR 手柄上 → 命中最近的 BR
        assert item._hit_handle(QPointF(hbr.x(), hbr.y())) == Handle.BR
        # 从 BR 手柄往对象外偏 60px → 不命中任何手柄（远离所有把手）
        far = inv.apply((br_scene[0] + 60 * scene_per_px,
                         br_scene[1] - 60 * scene_per_px))
        assert item._hit_handle(QPointF(*far)) == Handle.NONE
    w.close()


# ==================================================== 性能：防抖与跳过无变化
def test_global_panel_burst_debounces_to_single_apply(qapp):
    """按住 spinbox 连点时每个刻度不触发应用，停顿/flush 后只应用一次。"""
    w = _win(qapp)
    obj = make_text_object(TextSpec(text="Hi", font_names=["futural"],
                                    perturb=PerturbParams.natural(10.0)),
                           w.font_manager)
    w.controller.add_object(obj)
    w.canvas.sync_scene()
    w.canvas.select_object(obj)
    w.controller.selectionChanged.emit()
    fired = []
    w.global_perturb.paramsChanged.connect(lambda: fired.append(1))
    base = obj.source.data["perturb"]["char_y_sigma"]
    for dv in (0.1, 0.2, 0.3, 0.4, 0.5):
        w.global_perturb._spin["char_y_sigma"].setValue(base + dv)
    assert fired == []                       # 防抖窗口内不发
    w.global_perturb.flush()
    assert fired == [1]                      # 只发一次
    w.close()


def test_global_panel_identical_params_skip(qapp):
    """参数与对象当前值一致时不重复应用、不产生撤销项。"""
    w = _win(qapp)
    obj = make_text_object(TextSpec(text="Hi", font_names=["futural"],
                                    perturb=PerturbParams.natural(10.0)),
                           w.font_manager)
    w.controller.add_object(obj)
    w.canvas.sync_scene()
    w.canvas.select_object(obj)
    w.controller.selectionChanged.emit()
    w.global_perturb.flush()                 # 先同步一次面板参数
    n0 = w.controller.undo_stack.count()
    w.global_perturb.set_params(w.global_perturb.params())  # 相同参数回填
    w.global_perturb.flush()
    assert w.controller.undo_stack.count() == n0   # 无变化 → 不应用
    w.close()


def test_sync_scene_skips_unchanged_items(qapp):
    """全场景同步只重建笔画有变化的对象（大文档编辑不整篇重排）。"""
    w = _win(qapp)
    a = make_text_object(TextSpec(text="A", font_names=["futural"]),
                         w.font_manager)
    b = make_text_object(TextSpec(text="B", font_names=["futural"]),
                         w.font_manager)
    w.controller.add_object(a)
    w.controller.add_object(b)
    w.canvas.sync_scene()
    it_a, it_b = w.canvas._items[a.id], w.canvas._items[b.id]
    path_a0, path_b0 = it_a._path, it_b._path

    w.canvas.sync_scene()                    # 无变化：路径对象不变
    assert it_a._path is path_a0 and it_b._path is path_b0

    a.local_strokes = [s.clone() for s in a.local_strokes]
    w.canvas.sync_scene()                    # 只有 A 重建
    assert it_a._path is not path_a0
    assert it_b._path is path_b0

    b.transform = b.transform.translate(2.0, 0.0)
    w.canvas.sync_scene()                    # 变换不触发路径重建
    assert it_b._path is path_b0
    w.close()
