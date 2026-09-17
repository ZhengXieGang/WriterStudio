"""画布对象交互测试：移动/缩放/旋转的变换计算与撤销闭环。

直接驱动 ObjectGraphicsItem 的拖动逻辑（用轻量 stub 事件替代 Qt 事件），
避免依赖真实的鼠标事件分发，同时覆盖核心几何换算。
"""

from __future__ import annotations

import math
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtCore import QPointF  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from writerstudio.core.document import Document, make_static_object  # noqa: E402
from writerstudio.core.sample import make_rect  # noqa: E402
from writerstudio.core.geometry import AffineTransform  # noqa: E402
from writerstudio.ui.canvas import CanvasView  # noqa: E402
from writerstudio.ui.controller import DocumentController  # noqa: E402
from writerstudio.ui.items import Handle  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


class FakeEvent:
    """足以驱动 item 拖动逻辑的最小事件替身。

    真实 Qt 中 ``pos()``（本地坐标）由 ``scenePos()`` 经 item 当前变换反算得到，
    二者必须自洽。这里 ``local`` 省略时默认等于 ``scene``（本文件用例均为单位
    变换，scene == local）。
    """

    def __init__(self, scene, local=None, modifiers=0):
        self._scene = QPointF(*scene)
        self._local = QPointF(*(local if local is not None else scene))
        self._mod = modifiers
        self.accepted = False

    def scenePos(self):
        return self._scene

    def pos(self):
        return self._local

    def modifiers(self):
        return self._mod

    def button(self):
        from PySide6.QtCore import Qt
        return Qt.LeftButton

    def accept(self):
        self.accepted = True

    def ignore(self):
        self.accepted = False


_ALIVE = []  # 保持 view/scene 引用，防止 C++ 对象被 GC 提前销毁


def _make_item(qapp, transform=None):
    ctrl = DocumentController(Document())
    obj = make_static_object([make_rect(0.0, 0.0, 100.0, 50.0)], name="矩形",
                             transform=transform or AffineTransform.identity())
    ctrl.add_object(obj)
    view = CanvasView(ctrl)
    view.resize(600, 400)
    view.fit_page()
    _ALIVE.append(view)
    item = view._items[obj.id]
    return ctrl, obj, item


def test_move_drag_composes_translation(qapp):
    ctrl, obj, item = _make_item(qapp)
    item._begin_drag('move', None, FakeEvent((0, 0), (0, 0)))
    item._drag_kind = 'move'
    moved = FakeEvent((30, -10), (0, 0))
    delta = moved.scenePos() - item._start_scene
    new_t = AffineTransform.translate(delta.x(), delta.y()) @ item._start_transform
    item._preview(new_t)
    item.mouseReleaseEvent(moved)  # 提交
    assert obj.world_bbox().as_tuple() == (30.0, -10.0, 130.0, 40.0)


def test_scale_from_BR_keeps_TL_fixed(qapp):
    _, obj, item = _make_item(qapp)
    box = item._local_bbox()
    # Y 向上：本地「右下」手柄 BR = (right, top)，锚点为 TL = (left, bottom)
    br = (box.right(), box.top())
    item._begin_drag('handle', Handle.BR, FakeEvent(br))
    item._drag_kind = 'handle'
    item._mode = Handle.BR
    # 拖到 (200, -50)：BR 的锚点为对角 TL=(0,50)，故 x 与 y 均放大 2 倍
    ev = FakeEvent((200.0, -50.0))
    item._preview(item._compute_handle_transform(ev))
    item.mouseReleaseEvent(ev)  # 提交
    b = obj.world_bbox()
    # 以 y=50 为不动点放大 2 倍：y 0..50 → -50..50
    assert b.x0 == pytest.approx(0.0)
    assert b.y0 == pytest.approx(-50.0)
    assert b.x1 == pytest.approx(200.0)
    assert b.y1 == pytest.approx(50.0)
    assert b.width == pytest.approx(200.0)
    assert b.height == pytest.approx(100.0)


def test_rotate_handle_rotates_about_center(qapp):
    _, obj, item = _make_item(qapp)
    box = item._local_bbox()
    center = (box.center().x(), box.center().y())
    # 起始点取本地左下 (-50, -25)
    start = (box.left(), box.top())
    item._begin_drag('handle', Handle.ROTATE, FakeEvent(start))
    item._drag_kind = 'handle'
    item._mode = Handle.ROTATE
    # 目标点绕中心旋转 90°（本地逆时针）
    vx, vy = start[0] - center[0], start[1] - center[1]
    rx = vx * math.cos(math.pi / 2) - vy * math.sin(math.pi / 2)
    ry = vx * math.sin(math.pi / 2) + vy * math.cos(math.pi / 2)
    ev = FakeEvent((center[0] + rx, center[1] + ry))
    item._preview(item._compute_handle_transform(ev))
    item.mouseReleaseEvent(ev)  # 提交
    b = obj.world_bbox()
    # 100×50 旋转 90° → 50×100，中心不变 (50,25)
    assert b.width == pytest.approx(50.0)
    assert b.height == pytest.approx(100.0)
    assert b.center[0] == pytest.approx(50.0)
    assert b.center[1] == pytest.approx(25.0)


def test_drag_commit_pushes_undo(qapp):
    ctrl, obj, item = _make_item(qapp)
    original = obj.transform
    item._begin_drag('move', None, FakeEvent((0, 0), (0, 0)))
    item._drag_kind = 'move'
    item._preview(AffineTransform.translate(42.0, 42.0) @ original)
    item.mouseReleaseEvent(FakeEvent((0, 0), (0, 0)))
    assert obj.world_bbox().as_tuple()[0] == pytest.approx(42.0)
    assert ctrl.can_undo
    ctrl.undo()
    assert obj.transform == original
    ctrl.redo()
    assert obj.world_bbox().as_tuple()[0] == pytest.approx(42.0)


def test_selected_object_drags_from_empty_box_area(qapp):
    """已选中的对象：虚线框内空白处（非墨迹）按住即可拖动（PS 变换框语义）。"""
    ctrl, obj, item = _make_item(qapp)
    item.setSelected(True)
    # 矩形 100×50 的中心 (50,25) 离墨迹（边框）远，但在框内
    press = FakeEvent((50, 25))
    item.mousePressEvent(press)
    assert item._drag_kind == 'move'
    assert press.accepted
    move = FakeEvent((80, 5))
    item.mouseMoveEvent(move)
    item.mouseReleaseEvent(move)
    assert obj.world_bbox().as_tuple() == (30.0, -20.0, 130.0, 30.0)
    assert ctrl.can_undo


def test_unselected_object_press_off_ink_not_grabbed(qapp):
    """未选中的对象：框内空白处不放行（保留橡皮框选与点击下层对象）。"""
    _, obj, item = _make_item(qapp)
    assert not item.isSelected()
    press = FakeEvent((50, 25))          # 框内、离墨迹远
    item.mousePressEvent(press)
    assert item._drag_kind is None
    assert not press.accepted            # 已 ignore → 视图可起橡皮框
    assert not item.isSelected()
    # 但点在墨迹上仍能选中并进入拖动
    press2 = FakeEvent((0, 0))           # 矩形左下角顶点处
    item.mousePressEvent(press2)
    assert item._drag_kind == 'move'
    assert item.isSelected()


def test_canvas_background_cache_and_invalidation(qapp):
    """背景缓存开启；参考线开关/页面几何变化时失效。"""
    from PySide6.QtWidgets import QGraphicsView

    ctrl = DocumentController(Document())
    view = CanvasView(ctrl)
    view.resize(600, 400)
    _ALIVE.append(view)
    assert view.cacheMode() == QGraphicsView.CacheBackground

    calls = []
    view.resetCachedContent = lambda: calls.append(1)
    view.show_page_guide = True           # 值未变 → 不失效
    assert calls == []
    view.show_page_guide = False          # 变化 → 失效
    view.show_page_guide = False          # 重复赋同值 → 不再失效
    assert len(calls) == 1
    # 页面边距变化 → sync_scene → _update_scene_rect → 失效
    page = ctrl.doc.page
    page.margin = 25.0
    view.sync_scene()
    assert len(calls) == 2
    # 几何未再变化 → 不重复失效
    view.sync_scene()
    assert len(calls) == 2


def test_handle_hit_testing(qapp):
    _, obj, item = _make_item(qapp)
    item.setSelected(True)
    positions = item._handle_positions()
    # 直接命中 TL 手柄
    p = positions[Handle.TL]
    assert item._hit_handle(p) == Handle.TL
    # 命中旋转柄
    assert item._hit_handle(positions[Handle.ROTATE]) == Handle.ROTATE
    # 未选中时不响应手柄
    item.setSelected(False)
    assert item._hit_handle(positions[Handle.TL]) == Handle.NONE
