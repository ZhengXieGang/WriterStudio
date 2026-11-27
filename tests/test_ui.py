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
    # 关键控件可用：字体链至少可见 4 项，文本框至少 10 行
    assert dlg.chain_list.height() >= 100
    assert dlg.text_edit.height() >= 230
    dlg.close()


def test_text_dialog_accepts_extreme_content(qapp):
    """边界内容：空文本、单字符、数千字都不得抛异常且原样返回。"""
    from writerstudio.fonts.builder import TextSpec
    from writerstudio.fonts.manager import FontManager
    from writerstudio.ui.text_dialog import TextEditDialog

    dlg = TextEditDialog(TextSpec(text="测试", font_names=["futural"]),
                         FontManager())
    try:
        for text in ("", "A", "很长的文字" * 1000):
            dlg.text_edit.setPlainText(text)
            dlg._update_preview()
            assert dlg.result_spec().text == text
    finally:
        dlg.close()


# ========================================== UI 排版重构、跑马灯与滚轮保护回归
def test_status_card_marquee_single_line_and_hover_scroll(qapp):
    """机器状态卡片四项全部单行显示，超长时不换行也不撑宽；鼠标悬停后滚动。"""
    from PySide6.QtCore import QEvent, QPointF
    from PySide6.QtGui import QEnterEvent

    from writerstudio.settings import Settings, _MemoryBackend
    from writerstudio.ui.style import MarqueeLabel

    win = MainWindow(settings=Settings(backend=_MemoryBackend()))
    win.confirm_on_close = False
    try:
        win.resize(1280, 720)
        win.show()
        win.machine_dock.raise_()
        qapp.processEvents()
        mp = win.machine_panel
        labels = (mp.state_label, mp.pos_label, mp.feed_label, mp.fw_label)
        assert all(isinstance(w, MarqueeLabel) for w in labels)
        assert all(not w.wordWrap() for w in labels)

        h0 = mp.fw_label.height()
        long_text = "a 3000 mm/s² · X/Y 12000 mm/min · Z 12000 mm/min · GRBL 1.1h extended status"
        mp.fw_label.setText(long_text)
        qapp.processEvents()
        # 高度不变（单行不换行），且宽度充足能显示
        assert mp.fw_label.height() == h0
        assert mp.fw_label.width() >= 100

        # 模拟鼠标移入 → 启动跑马灯并推动偏移；移出 → 停止并复位
        enter = QEnterEvent(QPointF(4, 4), QPointF(4, 4), QPointF(4, 4))
        QApplication.sendEvent(mp.fw_label, enter)
        assert mp.fw_label._timer.isActive()
        for _ in range(mp.fw_label._PAUSE_TICKS + 5):
            mp.fw_label._on_tick()
        assert mp.fw_label._offset > 0.0

        QApplication.sendEvent(mp.fw_label, QEvent(QEvent.Type.Leave))
        assert not mp.fw_label._timer.isActive()
        assert mp.fw_label._offset == 0.0
    finally:
        win.close()


def test_inputs_ignore_wheel_until_focused(qapp):
    """旋钮/下拉/滑杆只有在选中聚焦时才响应滚轮，未聚焦时忽略事件透传滚屏。"""
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent

    from writerstudio.settings import Settings, _MemoryBackend

    win = MainWindow(settings=Settings(backend=_MemoryBackend()))
    win.confirm_on_close = False
    try:
        win.resize(1280, 720)
        win.show()
        win.machine_dock.raise_()
        qapp.processEvents()
        sp = win.machine_panel.jog_step
        combo = win.machine_panel.baud_combo
        slider = win.global_perturb.intensity_slider
        for w in (sp, combo, slider):
            assert w.focusPolicy() == Qt.FocusPolicy.StrongFocus

        # 确保未聚焦
        win.canvas.setFocus()
        qapp.processEvents()
        assert not sp.hasFocus()
        before_val = sp.value()
        ev1 = QWheelEvent(
            QPointF(10, 10), QPointF(10, 10),
            QPoint(0, 0), QPoint(0, 120),
            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.NoScrollPhase, False,
        )
        QApplication.sendEvent(sp, ev1)
        assert sp.value() == pytest.approx(before_val)
        assert not ev1.isAccepted()

        # 显式选中聚焦后再滚轮 → 正常调整数值
        sp.setFocus()
        qapp.processEvents()
        assert sp.hasFocus()
        ev2 = QWheelEvent(
            QPointF(10, 10), QPointF(10, 10),
            QPoint(0, 0), QPoint(0, 120),
            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.NoScrollPhase, False,
        )
        QApplication.sendEvent(sp, ev2)
        assert sp.value() > before_val
    finally:
        win.close()


def test_machine_panel_refresh_button_inside_bounds_and_no_nested_scroll(qapp):
    """串口刷新按钮不得越出父框右边界；机器面板与内容弹窗无嵌套 QScrollArea。"""
    from PySide6.QtWidgets import QScrollArea

    from writerstudio.settings import Settings, _MemoryBackend
    from writerstudio.ui.content_dialog import ContentDialog, KIND_MARKDOWN

    win = MainWindow(settings=Settings(backend=_MemoryBackend()))
    win.confirm_on_close = False
    try:
        win.resize(1280, 720)
        win.show()
        win._apply_default_dock_widths()
        win.machine_dock.raise_()
        qapp.processEvents()

        # 串口刷新按钮完整留在父容器内
        btn = win.machine_panel._refresh_btn
        assert btn.geometry().right() <= btn.parentWidget().width()

        # 机器停靠区只有一个 QScrollArea（无双层嵌套）
        assert len(win.machine_dock.findChildren(QScrollArea)) == 1

        # 分段切换「控制 / 参数」后底部常驻发送/暂停/中止均可见
        win.machine_panel._switch_tab(1)
        qapp.processEvents()
        assert win.machine_panel.send_btn.isVisible()
        assert win.machine_panel.abort_btn.isVisible()

        # 富内容对话框中不存在 QScrollArea 嵌套 QScrollArea
        cdlg = ContentDialog(win.font_manager, initial_kind=KIND_MARKDOWN, parent=win)
        try:
            for sa in cdlg.findChildren(QScrollArea):
                p = sa.parentWidget()
                while p is not None and p is not cdlg:
                    assert not isinstance(p, QScrollArea)
                    p = p.parentWidget()
        finally:
            cdlg.close()
    finally:
        win.close()


def test_menu_order_lists_and_dialog_alignment(qapp):
    """帮助菜单在最右；列表禁用横向滚动条；页面设置对齐无台阶且弹窗高度统一。"""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QDialogButtonBox

    from writerstudio.ai.dialog import AiServiceDialog
    from writerstudio.settings import Settings, _MemoryBackend
    from writerstudio.ui.page_dialog import PagePresetEditorDialog, PageSetupDialog

    win = MainWindow(settings=Settings(backend=_MemoryBackend()))
    win.confirm_on_close = False
    try:
        win.show()
        qapp.processEvents()
        menus = [a.text().replace("&", "") for a in win.menuBar().actions()]
        assert menus[-1] in ("帮助(H)", "Help(H)")

        for lw in (win.undo_panel.list, win.obj_list,
                   win.reference_panel.list, win.font_panel.list):
            assert lw.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
            assert lw.textElideMode() == Qt.TextElideMode.ElideRight

        # 页面设置表单左缘对齐
        pdlg = PageSetupDialog(win.controller.doc.page, win.page_presets, True, win)
        try:
            pdlg.show()
            qapp.processEvents()
            assert abs(pdlg.preset_combo.x() - pdlg.margin_spin.x()) <= 2
            assert abs(pdlg.orient_combo.x() - pdlg.margin_spin.x()) <= 2
        finally:
            pdlg.close()

        # 预设编辑器可编辑下拉框与旋钮等高
        pedlg = PagePresetEditorDialog(win.page_presets, parent=win)
        try:
            pedlg.show()
            qapp.processEvents()
            assert pedlg.name_edit.height() == pedlg.margin_spin.height()
        finally:
            pedlg.close()

        # AI 服务对话框带关闭按钮
        adlg = AiServiceDialog(win)
        try:
            boxes = adlg.findChildren(QDialogButtonBox)
            assert boxes and boxes[0].button(QDialogButtonBox.StandardButton.Close) is not None
        finally:
            adlg.close()
    finally:
        win.close()


def test_light_theme_field_border_and_canvas_bg(qapp):
    """浅色主题为输入控件注入可见边框，画布背景色随主题切换。"""
    from writerstudio.settings import Settings, _MemoryBackend
    from writerstudio.ui.theme import THEME_DARK, THEME_LIGHT, apply_theme, available

    if not available():
        pytest.skip("qdarktheme 未安装")
    win = MainWindow(settings=Settings(backend=_MemoryBackend()))
    win.confirm_on_close = False
    try:
        win._apply_theme_setting(THEME_DARK)
        dark_bg = win.canvas.backgroundBrush().color()
        win._apply_theme_setting(THEME_LIGHT)
        light_bg = win.canvas.backgroundBrush().color()
        assert light_bg != dark_bg
        assert light_bg.lightness() > dark_bg.lightness()
        qss = qapp.styleSheet()
        assert 'wsField="true"' in qss
        assert "border: 1px solid transparent" not in qss
    finally:
        win.close()
        apply_theme(qapp, THEME_DARK)
        qapp.setStyleSheet("")
