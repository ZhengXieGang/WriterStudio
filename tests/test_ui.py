"""UI 冒烟测试：验证主窗口可构建、画布可同步、撤销/重做闭环。

使用 Qt 的 offscreen 平台（CI/无显示器环境也可运行）。
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from writerstudio.core.document import make_static_object  # noqa: E402
from writerstudio.core.geometry import AffineTransform  # noqa: E402
from writerstudio.core.sample import demo_document, make_rect  # noqa: E402
from writerstudio.ui.canvas import CanvasView  # noqa: E402
from writerstudio.ui.controller import DocumentController  # noqa: E402
from writerstudio.ui.main_window import MainWindow  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_main_window_constructs(qapp):
    win = MainWindow()
    assert win.controller.doc is not None
    # 启动为空白文档：不再预置演示图形
    assert len(win.controller.doc) == 0
    assert win.obj_list.count() == len(win.controller.doc)
    win.close()


def test_canvas_zoom_and_fit(qapp):
    ctrl = DocumentController(demo_document())
    view = CanvasView(ctrl)
    view.resize(800, 600)
    view.fit_page()
    z0 = view.current_zoom()
    assert z0 > 0
    view.zoom_in()
    assert view.current_zoom() > z0
    view.zoom_out()
    assert abs(view.current_zoom() - z0) < z0 * 0.01


def test_controller_undo_redo_transform(qapp):
    ctrl = DocumentController(demo_document())
    obj = ctrl.doc.objects[0]
    original = obj.transform

    ctrl.set_transform(obj, AffineTransform.translate(25.0, 0.0) @ original, "移动")
    assert obj.transform.apply((0.0, 0.0))[0] == pytest.approx(25.0 + original.apply((0.0, 0.0))[0])

    ctrl.undo()
    assert obj.transform == original

    ctrl.redo()
    assert obj.transform.apply((0.0, 0.0))[0] == pytest.approx(25.0 + original.apply((0.0, 0.0))[0])


def test_controller_add_remove_undo(qapp):
    ctrl = DocumentController()
    n0 = len(ctrl.doc)
    obj = make_static_object([make_rect(0, 0, 5, 5)], name="新矩形")
    ctrl.add_object(obj)
    assert len(ctrl.doc) == n0 + 1
    ctrl.undo()
    assert len(ctrl.doc) == n0
    ctrl.redo()
    assert len(ctrl.doc) == n0 + 1

    ctrl.remove_object(obj)
    assert len(ctrl.doc) == n0
    ctrl.undo()
    assert len(ctrl.doc) == n0 + 1


def test_canvas_scene_syncs_on_change(qapp):
    ctrl = DocumentController()
    view = CanvasView(ctrl)
    assert len(view._items) == 0
    obj = make_static_object([make_rect(0, 0, 5, 5)], name="方块")
    ctrl.add_object(obj)
    assert obj.id in view._items
    ctrl.remove_object(obj)
    assert obj.id not in view._items


# ============================================== UI 尺寸：面板/对话框不被撑爆
def test_main_window_docks_fit_default_widths(qapp):
    """各停靠面板内容最小宽度不得超过默认停靠宽度。

    回归：字体面板五个按钮挤一行把最小宽撑到 ~456px、扰动面板顶行
    ~336px——Qt 会强制把整列撑宽，画布被挤出画面外。
    """
    win = MainWindow()
    win.show()
    qapp.processEvents()
    limits = [
        (win.font_panel, 280),
        (win.perturb_dock, 280),
        (win.reference_dock, 280),
        (win.machine_dock, 300),
    ]
    for dock, want in limits:
        w = dock.widget()
        assert w is not None
        assert w.minimumSizeHint().width() <= want, \
            f"{dock.windowTitle()} 内容最小宽 {w.minimumSizeHint().width()} > {want}"
    win.close()


def test_machine_panel_progress_text_does_not_widen_panel(qapp):
    """发送中的长进度/提醒文字不得把面板内容撑宽（应换行，出现的是
    横向滚动条、内容右侧被挤出画面外——回归）。"""
    from PySide6.QtWidgets import QScrollArea

    win = MainWindow()
    win.show()
    qapp.processEvents()
    body = None
    for c in win.machine_panel.children():
        if isinstance(c, QScrollArea):
            body = c.widget()
    assert body is not None
    before = body.minimumSizeHint().width()
    win.machine_panel.set_progress(
        "发送中：35%（1234/4567 行）  已用 5:32  预计还需 8:10")
    win.machine_panel._update_penlife_label()
    qapp.processEvents()
    after = body.minimumSizeHint().width()
    assert after <= before, f"长文字把面板最小宽从 {before} 撑到 {after}"
    win.close()


def test_text_dialog_opens_without_squeeze(qapp):
    """文本编辑对话框按默认尺寸打开时不得挤压控件。

    回归：默认高度 580 低于内容真实最小需求 654，字体列表被压到
    37px（只见一项）、文本框被压到 87px（两三行）。
    """
    from writerstudio.fonts.builder import TextSpec
    from writerstudio.fonts.manager import FontManager
    from writerstudio.ui.text_dialog import TextEditDialog

    m = FontManager()
    dlg = TextEditDialog(TextSpec(text="测试", font_names=["futural"]), m)
    dlg.show()
    qapp.processEvents()
    # 无溢出：显示尺寸 ≥ 布局最小需求
    assert dlg.geometry().height() >= dlg.minimumSizeHint().height()
    # 关键控件可用：字体链至少可见 4 项，文本框至少 6 行
    assert dlg.chain_list.height() >= 100
    assert dlg.text_edit.height() >= 140
    dlg.close()
