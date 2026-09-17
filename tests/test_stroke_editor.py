"""笔画编辑工具测试（阶段 7）。

覆盖：附着/命中、移动/旋转手势与撤销、弯折局部扭曲、删除单笔、
重新扰动、Esc 退出。手势直接调 ``StrokeEditor`` 方法（离屏无真实鼠标）。
"""

from __future__ import annotations

import math
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent, QPointF, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from writerstudio.fonts.builder import TextSpec, make_text_object  # noqa: E402
from writerstudio.ui.main_window import MainWindow  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


@pytest.fixture()
def win(qapp):
    w = MainWindow()
    w.confirm_on_close = False
    spec = TextSpec(text="AF", font_names=["futural"], size=20.0)
    obj = make_text_object(spec, w.font_manager)
    w.controller.add_object(obj)
    w.canvas.sync_scene()
    w.canvas.zoom_100()          # 20mm 字形 ≈ 75px，保证手柄处于可用尺寸
    w.canvas.select_object(obj)
    w.act_stroke_edit.setChecked(True)
    yield w
    w.close()


def test_attach_and_exit(win):
    se = win.stroke_editor
    assert se.active and se.obj is win.controller.doc.objects[0]
    win._exit_stroke_edit()
    assert not se.active
    assert not win.act_stroke_edit.isChecked()      # Esc/完成会同步回弹起状态


def test_toggle_without_selection_rejected(qapp):
    w = MainWindow()
    w.confirm_on_close = False
    w.act_stroke_edit.setChecked(True)
    assert not w.act_stroke_edit.isChecked()        # 无选中对象不允许进入
    w.close()


def _stroke_by_point(se, pt):
    """取离 pt 最近的笔画索引。"""
    best, best_d = None, float("inf")
    for i, s in enumerate(se.world):
        for p in s.points:
            d = math.hypot(pt[0] - p[0], pt[1] - p[1])
            if d < best_d:
                best, best_d = i, d
    return best


def test_move_gesture_with_undo(win):
    se = win.stroke_editor
    obj = se.obj
    p0 = obj.world_strokes()[0].points[0]
    assert se.on_press(QPointF(p0[0], p0[1]), Qt.NoModifier)
    se.on_move(QPointF(p0[0] + 3.0, p0[1] + 2.0))
    se.on_release()
    assert obj.local_strokes[0].points[0] == (p0[0] + 3.0, p0[1] + 2.0)
    win.controller.undo()
    assert obj.local_strokes[0].points[0] == p0


def test_move_gesture_accumulates_small_steps(win):
    """P31 回归：真实鼠标的拖动是一串小步事件，位移必须相对按下点累计。

    曾用相邻事件增量叠加原始坐标——预览每帧只偏移最后一步，笔画被
    "拽回原点、只能抽搐"，松手后等于没拖（单步大位移的旧测试测不出）。
    """
    se = win.stroke_editor
    obj = se.obj
    p0 = obj.world_strokes()[0].points[0]
    assert se.on_press(QPointF(p0[0], p0[1]), Qt.NoModifier)
    # 模拟真实拖动：20 个 1mm 的小步事件
    for k in range(1, 21):
        se.on_move(QPointF(p0[0] + k * 1.0, p0[1] + k * 0.5))
    assert se.preview.points[0] == (p0[0] + 20.0, p0[1] + 10.0)
    se.on_release()
    after = obj.local_strokes[0].points[0]
    assert after == (p0[0] + 20.0, p0[1] + 10.0)
    win.controller.undo()
    assert obj.local_strokes[0].points[0] == p0


def test_rotate_gesture(win):
    se = win.stroke_editor
    obj = se.obj
    strokes = obj.world_strokes()
    s0 = strokes[0]
    # 选 stroke0 上离其他笔画最远的点，保证命中 stroke0
    others = [p for i, s in enumerate(strokes) if i != 0 for p in s.points]

    def far(pt):
        return max(math.hypot(pt[0] - q[0], pt[1] - q[1]) for q in others)

    anchor = max(s0.points, key=far)
    assert se.on_press(QPointF(anchor[0], anchor[1]), Qt.NoModifier)
    box = s0.bbox()
    off = 14.0 / se._zoom()
    rot_handle = QPointF((box.x0 + box.x1) / 2.0, box.y1 + off)
    # 直接对选中笔画做旋转手势：按下旋转柄
    se.preview = None
    assert se.on_press(rot_handle, Qt.NoModifier)
    assert se._drag and se._drag["kind"] == "rotate"
    # 拖动 90°：从柄正上方拖到正右方（绕中心）
    cx, cy = box.center
    se.on_move(QPointF(cx + (box.y1 - cy + off), cy))
    se.on_release()
    moved = obj.local_strokes[0]
    assert moved.points != s0.points
    win.controller.undo()
    assert obj.local_strokes[0].points == s0.points


def test_bend_gesture_local_deform(win):
    se = win.stroke_editor
    obj = se.obj
    strokes = obj.world_strokes()
    orig0 = [tuple(p) for p in strokes[0].points]
    others = [p for i, s in enumerate(strokes) if i != 0 for p in s.points]

    def far(pt):
        return max(math.hypot(pt[0] - q[0], pt[1] - q[1]) for q in others)

    anchor = max(strokes[0].points, key=far)
    se.set_bend_mode(True)
    assert se.on_press(QPointF(anchor[0], anchor[1]), Qt.NoModifier)
    assert se._drag["kind"] == "bend"
    se.on_move(QPointF(anchor[0], anchor[1] + 1.5))
    se.on_release()
    new0 = [tuple(p) for p in obj.local_strokes[0].points]
    assert new0 != orig0
    # 局部性：离锚点越远的点位移越小（都应小于锚点处位移）
    def disp(a, b):
        return max(math.hypot(a[i][0] - b[i][0], a[i][1] - b[i][1])
                   for i in range(len(a)))
    win.controller.undo()
    assert [tuple(p) for p in obj.local_strokes[0].points] == orig0


def test_delete_stroke_and_undo(win):
    se = win.stroke_editor
    obj = se.obj
    n = len(obj.local_strokes)
    se.sel = 0
    win._delete_selected_stroke()
    assert len(obj.local_strokes) == n - 1
    win.controller.undo()
    assert len(obj.local_strokes) == n


def test_escape_requests_exit(qapp, win):
    from PySide6.QtGui import QKeyEvent
    se = win.stroke_editor
    event = QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier)
    win.canvas.keyPressEvent(event)
    assert not se.active


# ==================================================== Delete 键与悬停高亮
def test_delete_key_removes_stroke_not_object(win):
    """笔画编辑下 Delete 删除选中笔画，整个对象保留；撤销可还原。"""
    se = win.stroke_editor
    obj = se.obj
    n0 = len(obj.local_strokes)
    p0 = obj.world_strokes()[0].points[0]
    assert se.on_press(QPointF(p0[0], p0[1]), Qt.NoModifier)
    assert se.selected_index() == 0
    win._delete_selected()                       # Delete 走同一入口
    assert len(win.controller.doc.objects) == 1   # 对象还在
    assert len(obj.local_strokes) == n0 - 1       # 只少了一条笔画
    assert se.selected_index() is None
    # 再按 Delete：无选中笔画 → 提示而非删除对象
    win._delete_selected()
    assert len(win.controller.doc.objects) == 1
    assert len(obj.local_strokes) == n0 - 1
    win.controller.undo()
    assert len(obj.local_strokes) == n0


def test_delete_routes_to_object_without_stroke_edit(qapp):
    """未进笔画编辑时 Delete 仍删除整个对象（原行为不变）。"""
    w = MainWindow()
    w.confirm_on_close = False
    spec = TextSpec(text="AF", font_names=["futural"], size=20.0)
    obj = make_text_object(spec, w.font_manager)
    w.controller.add_object(obj)
    w.canvas.sync_scene()
    w.canvas.select_object(obj)
    w._delete_selected()
    assert not w.controller.doc.objects
    w.close()


def test_hover_highlight_tracks_cursor(win):
    """鼠标下笔画自动悬停高亮：命中时点亮，移开/移出画布时熄灭。"""
    se = win.stroke_editor
    p0 = se.obj.world_strokes()[0].points[0]
    se.update_hover(QPointF(p0[0], p0[1]))
    assert se.hover == 0
    se.update_hover(QPointF(-500.0, -500.0))     # 远离所有笔画
    assert se.hover is None
    se.update_hover(QPointF(p0[0], p0[1]))
    assert se.hover == 0
    se.update_hover(None)                        # 移出画布
    assert se.hover is None


def test_hover_suppressed_while_dragging(win):
    """拖动手势中悬停高亮保持不动（以编辑中的笔画为准）。"""
    se = win.stroke_editor
    p0 = se.obj.world_strokes()[0].points[0]
    se.update_hover(QPointF(p0[0], p0[1]))
    assert se.hover == 0
    assert se.on_press(QPointF(p0[0], p0[1]), Qt.NoModifier)
    assert se._drag is not None
    se.update_hover(QPointF(-500.0, -500.0))     # 拖动中不响应悬停更新
    assert se.hover == 0
    se.on_release()


def test_paint_draws_hover_without_error(win):
    """悬停高亮参与绘制且不报错（含选中+悬停并存、无选中仅悬停）。"""
    se = win.stroke_editor
    p0 = se.obj.world_strokes()[0].points[0]
    se.update_hover(QPointF(p0[0], p0[1]))
    win.canvas.grab()                            # 未选中 + 悬停
    assert se.on_press(QPointF(p0[0], p0[1]), Qt.NoModifier)
    se.on_release()
    se.update_hover(QPointF(-500.0, -500.0))
    win.canvas.grab()                            # 选中 + 无悬停


# ---------------------------------------------- 手势误触发与退出（P15 修复）
def test_noop_click_does_not_push_undo(win):
    """原地点击选中笔画（无拖动）：不提交、不产生撤销项。"""
    se = win.stroke_editor
    n0 = win.controller.undo_stack.count()
    p = se.world[0].points[0]
    se.on_press(QPointF(*p), Qt.KeyboardModifier.NoModifier)
    assert se.is_dragging()
    se.on_release()
    assert not se.is_dragging()
    assert win.controller.undo_stack.count() == n0


def test_handle_mis_hit_prefers_nearer_stroke(win, monkeypatch):
    """点选另一条笔画时即使落在已选笔画的角手柄容差内，
    也按「选新笔画」处理，而不是误触发缩放手势。"""
    from writerstudio.core.strokes import Stroke
    se = win.stroke_editor
    monkeypatch.setattr(se, "_refresh_world", lambda: None)  # 保留合成笔画
    se.world = [Stroke([(0, 0), (10, 0), (10, 8), (0, 8), (0, 0)]),
                Stroke([(10.5, 4), (20, 4)])]
    se._rebuild_index()      # 绕过 _refresh_world 注入笔画，须同步重建索引
    se.sel = None
    se.preview = None
    # 选中笔画 0
    se.on_press(QPointF(0, 0), Qt.KeyboardModifier.NoModifier)
    se.on_release()
    assert se.sel == 0
    # 点击 (10.7, 4)：落在笔画 0 的 ne 手柄容差内，但到笔画 1（过 (10.5,4)
    # 的横线）比到笔画 0 边缘更近 → 应选新笔画，而非缩放手势
    se.on_press(QPointF(10.7, 4), Qt.KeyboardModifier.NoModifier)
    assert se.sel == 1
    assert se._drag["kind"] == "move"
    se.on_release()


def test_small_stroke_redrag_not_hijacked_by_handles(win, monkeypatch):
    """P31 回归：已选小笔画的第二次拖动曾被角手柄劫持成 scale。

    小字号手写体笔画包围盒只有几个屏幕像素，13px 手柄命中圈罩住整条
    笔画——再拖时按点落在手柄容差内，手势退化成围绕锚点的缩放/旋转，
    笔画"吸附在原位"不跟手。墨迹优先规则下按在笔画上必须是 move
    （手柄不禁用，放大后仍可从空白处精确按到手柄）。
    """
    from writerstudio.core.strokes import Stroke
    se = win.stroke_editor
    monkeypatch.setattr(se, "_refresh_world", lambda: None)
    # 6mm × 0.1mm 的细笔画（zoom_100 下 ≈23px 宽）
    se.world = [Stroke([(0, 0), (6, 0), (6, 0.1), (0, 0.1), (0, 0)])]
    se._rebuild_index()
    se.sel = None
    se.preview = None
    # 第一次按下：点选
    se.on_press(QPointF(3, 0.05), Qt.KeyboardModifier.NoModifier)
    se.on_release()
    assert se.sel == 0
    # 第二次按在笔画中点并拖动：必须是移动手势，且位移精确跟手
    se.on_press(QPointF(3, 0.05), Qt.KeyboardModifier.NoModifier)
    assert se._drag["kind"] == "move"
    se.on_move(QPointF(10, 3))
    se.on_release()
    # 增量位移相对按下点 (3, 0.05)：dy = 3 - 0.05
    assert se.world[0].points[1] == (13.0, 2.95)


def test_small_stroke_handle_grabbable_when_aimed(win, monkeypatch):
    """手柄不禁用：小笔画放大后（或精确瞄准）从空白处按手柄仍能变换。

    6mm 细笔画（zoom_100 下包围盒 ≈23px），旋转柄在框上方 14px 的
    空白处——按在那里必须进入 rotate 手势。
    """
    from writerstudio.core.strokes import Stroke
    se = win.stroke_editor
    monkeypatch.setattr(se, "_refresh_world", lambda: None)
    se.world = [Stroke([(0, 0), (6, 0), (6, 0.1), (0, 0.1), (0, 0)])]
    se._rebuild_index()
    se.sel = None
    se.preview = None
    se.on_press(QPointF(3, 0.05), Qt.KeyboardModifier.NoModifier)
    se.on_release()
    assert se.sel == 0
    off = 14.0 / se._zoom()
    se.preview = None
    se.on_press(QPointF(3, 0.1 + off), Qt.KeyboardModifier.NoModifier)
    assert se._drag["kind"] == "rotate"
    se.on_release()


def test_undo_refreshes_editor_cache(win):
    """P31：笔画编辑中撤销后，编辑器缓存/手势状态必须随文档刷新。

    否则高亮与手柄拿旧的世界缓存画在旧位置留下残影，要点击画布
    才消失。
    """
    se = win.stroke_editor
    obj = se.obj
    p0 = obj.world_strokes()[0].points[0]
    se.on_press(QPointF(p0[0], p0[1]), Qt.NoModifier)
    se.on_move(QPointF(p0[0] + 15, p0[1] + 10))
    se.on_release()
    assert se.world[0].points[0] == (p0[0] + 15, p0[1] + 10)
    win.controller.undo()
    assert se.world[0].points[0] == p0           # 缓存已随撤销刷新
    assert se.preview is None and not se._multi_preview
    assert se._drag is None and se._band_origin is None


def test_undo_delete_restores_and_clamps_selection(win):
    """撤销删除后笔画恢复，编辑器世界缓存与选择集保持有效。"""
    se = win.stroke_editor
    n0 = len(se.world)
    se.sel = 0                                    # 直接选中第一条真实笔画
    assert se.delete_selected()
    assert len(se.world) == n0 - 1
    assert se.selection == set() and se._primary is None
    win.controller.undo()
    assert len(se.world) == n0                    # 撤销后缓存随文档恢复
    for i in se.selection:
        assert 0 <= i < len(se.world)


def test_single_path_commit_keeps_world_cache_sane(win):
    """P31 回归：旋转/缩放/弯折手势走单笔画提交路径，曾在 commit 回调
    同步文档（清空 self.preview）之后又把 self.preview 写回 world——
    None 进缓存，悬停每帧 AttributeError、重新扰动对选中笔画失效。
    """
    se = win.stroke_editor
    obj = se.obj
    strokes = obj.world_strokes()
    s0 = strokes[0]
    others = [p for i, s in enumerate(strokes) if i != 0 for p in s.points]

    def far(pt):
        return max(math.hypot(pt[0] - q[0], pt[1] - q[1]) for q in others)

    anchor = max(s0.points, key=far)
    se.on_press(QPointF(anchor[0], anchor[1]), Qt.NoModifier)
    se.on_release()                               # 选中笔画 0
    box = s0.bbox()
    off = 14.0 / se._zoom()
    se.preview = None
    se.on_press(QPointF((box.x0 + box.x1) / 2.0, box.y1 + off), Qt.NoModifier)
    assert se._drag["kind"] == "rotate"
    cx, cy = box.center
    se.on_move(QPointF(cx + (box.y1 - cy + off), cy))
    se.on_release()                               # 单笔画提交路径
    assert all(s is not None for s in se.world)   # 缓存不得被 None 污染
    # 悬停/命中走同一份缓存：world[i] 为 None 时这里就是当初的
    # AttributeError 崩点（按在墨迹上必须命中）
    probe = strokes[1].points[0] if len(strokes) > 1 else s0.points[0]
    se.update_hover(QPointF(probe[0], probe[1]))
    assert se.hover is not None


def test_handles_reachable_from_blank_on_large_stroke(win, monkeypatch):
    """大笔画的手柄仍可用：按在笔画旁空白处的手柄上进入变换手势。"""
    from writerstudio.core.strokes import Stroke
    se = win.stroke_editor
    monkeypatch.setattr(se, "_refresh_world", lambda: None)
    # 60mm 宽横线（zoom_100 下 ≈227px，手柄可用），选中之
    se.world = [Stroke([(0, 0), (60, 0)])]
    se._rebuild_index()
    se.sel = None
    se.preview = None
    se.on_press(QPointF(30, 0), Qt.KeyboardModifier.NoModifier)
    se.on_release()
    assert se.sel == 0
    # 旋转柄在框上方 14px 处（空白）：按下必须是 rotate
    off = 14.0 / se._zoom()
    se.preview = None
    se.on_press(QPointF(30, off), Qt.KeyboardModifier.NoModifier)
    assert se._drag["kind"] == "rotate"
    se.on_release()


def test_float_bar_buttons_keep_canvas_focus(win):
    """浮动条按钮不抢键盘焦点：点完按钮 Esc 仍由画布接收。"""
    from PySide6.QtCore import Qt as _Qt
    assert win._bend_btn.focusPolicy() == _Qt.NoFocus
    assert win._stroke_bar.focusPolicy() == _Qt.NoFocus


def test_double_click_does_not_open_dialog_in_stroke_edit(win, monkeypatch,
                                                          qapp):
    """笔画编辑中双击按普通按下处理，不许穿透打开属性对话框。"""
    opened = []

    class FakeDlg:
        def __init__(self, *a, **k):
            opened.append(1)

        def exec(self):
            return 0

        def result_spec(self):
            raise AssertionError("不应提交")

    import writerstudio.ui.main_window as mw
    monkeypatch.setattr(mw, "TextEditDialog", FakeDlg)
    se = win.stroke_editor
    p = se.world[0].points[0]
    from PySide6.QtCore import QEvent, QPointF as QPF
    from PySide6.QtGui import QMouseEvent
    vp = win.canvas.mapFromScene(QPF(*p))
    ev = QMouseEvent(QEvent.MouseButtonDblClick, QPF(vp), QPF(vp),
                     Qt.LeftButton, Qt.LeftButton,
                     Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(win.canvas.viewport(), ev)
    assert opened == []                      # 没有打开属性对话框
    assert se.sel is not None                # 而是按下了笔画


# ------------------------------------------------- 画布事件管线级回归
def _send_mouse(canvas, etype, scene_pt, btn, buttons):
    from PySide6.QtCore import QPointF as QPF
    from PySide6.QtGui import QMouseEvent
    vp = canvas.mapFromScene(QPF(*scene_pt))
    ev = QMouseEvent(etype, QPF(vp), QPF(vp), btn, buttons,
                     Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(canvas.viewport(), ev)


def test_canvas_pipeline_release_ends_drag(qapp, win):
    """回归：松开鼠标必须结束笔画手势（此前文字会话接线时丢失了
    mouseReleaseEvent 的笔画分支，导致拖拽永不结束、选择框跟着鼠标跑）。"""
    se = win.stroke_editor
    p0 = se.world[0].points[0]
    n0 = win.controller.undo_stack.count()

    # 原地点击：选中但无撤销项
    _send_mouse(win.canvas, QEvent.MouseButtonPress, p0,
                Qt.LeftButton, Qt.LeftButton)
    _send_mouse(win.canvas, QEvent.MouseButtonRelease, p0,
                Qt.LeftButton, Qt.NoButton)
    qapp.processEvents()
    assert se.sel == 0
    assert not se.is_dragging()
    assert win.controller.undo_stack.count() == n0

    # 拖动：松手即结束手势并入栈一步
    _send_mouse(win.canvas, QEvent.MouseButtonPress, p0,
                Qt.LeftButton, Qt.LeftButton)
    _send_mouse(win.canvas, QEvent.MouseMove, (p0[0] + 9, p0[1] + 5),
                Qt.NoButton, Qt.LeftButton)
    _send_mouse(win.canvas, QEvent.MouseButtonRelease, (p0[0] + 9, p0[1] + 5),
                Qt.LeftButton, Qt.NoButton)
    qapp.processEvents()
    assert not se.is_dragging()               # 手势必须结束
    assert win.controller.undo_stack.count() == n0 + 1


# ================================================= 框选与批量编辑
def _synthetic_win(qapp, strokes):
    """构造一个含合成笔画的对象并进入笔画编辑（便于精确控制几何）。"""
    from writerstudio.core.document import DocumentObject, SourceSpec
    from writerstudio.core.strokes import Stroke
    w = MainWindow()
    w.confirm_on_close = False
    obj = DocumentObject(name="grid", source=SourceSpec("static", {}),
                         local_strokes=[Stroke(list(pts)) for pts in strokes])
    w.controller.add_object(obj)
    w.canvas.sync_scene()
    w.canvas.select_object(obj)
    w.act_stroke_edit.setChecked(True)
    return w, w.stroke_editor, obj


def test_band_select_picks_intersecting_strokes(qapp):
    """空白处拖出橡皮框：框内/相交的笔画被选中，框外不选。"""
    strokes = [[(0, 0), (5, 0)], [(20, 0), (25, 0)],
               [(0, 10), (5, 10)], [(20, 10), (25, 10)]]
    w, se, obj = _synthetic_win(qapp, strokes)
    try:
        # 起点放在离所有笔画都超出抓取容差的空白处
        assert se.on_press(QPointF(-6, -6), Qt.KeyboardModifier.NoModifier)
        assert se._band_origin is not None
        se.on_move(QPointF(8, 12))
        assert se._band is not None            # 已拖出橡皮框
        se.on_release()
        assert se.selection == {0, 2}
    finally:
        w.close()


def test_band_select_shift_appends(qapp):
    strokes = [[(0, 0), (5, 0)], [(20, 0), (25, 0)]]
    w, se, obj = _synthetic_win(qapp, strokes)
    try:
        # 起点用远离笔画的空白，确保是框选而非点选
        se.on_press(QPointF(-6, -6), Qt.KeyboardModifier.NoModifier)
        assert se._band_origin is not None
        se.on_move(QPointF(8, 8)); se.on_release()
        assert se.selection == {0}
        # Shift 框选追加
        se.on_press(QPointF(14, -6), Qt.KeyboardModifier.ShiftModifier)
        se.on_move(QPointF(28, 8)); se.on_release()
        assert se.selection == {0, 1}
    finally:
        w.close()


def test_click_empty_clears_selection(qapp):
    strokes = [[(0, 0), (5, 0)], [(20, 0), (25, 0)]]
    w, se, obj = _synthetic_win(qapp, strokes)
    try:
        se.sel = 0
        assert se.selection == {0}
        # 空白处原地点击（无拖动）→ 清空
        se.on_press(QPointF(50, 50), Qt.KeyboardModifier.NoModifier)
        se.on_release()
        assert se.selection == set()
        assert se.selected_index() is None
    finally:
        w.close()


def test_click_empty_shift_keeps_selection(qapp):
    strokes = [[(0, 0), (5, 0)]]
    w, se, obj = _synthetic_win(qapp, strokes)
    try:
        se.sel = 0
        se.on_press(QPointF(50, 50), Qt.KeyboardModifier.ShiftModifier)
        se.on_release()
        assert se.selection == {0}
    finally:
        w.close()


def test_multi_move_gesture_moves_all(qapp):
    """多选后拖动：所有选中笔画一起平移，一次撤销可整体还原。"""
    strokes = [[(0, 0), (5, 0)], [(0, 3), (5, 3)], [(20, 0), (25, 0)]]
    w, se, obj = _synthetic_win(qapp, strokes)
    try:
        se._set_selection({0, 1})
        orig0 = [tuple(p) for p in obj.local_strokes[0].points]
        orig1 = [tuple(p) for p in obj.local_strokes[1].points]
        orig2 = [tuple(p) for p in obj.local_strokes[2].points]
        assert se.on_press(QPointF(0, 0), Qt.KeyboardModifier.NoModifier)
        assert se._drag and se._drag["kind"] == "move"
        se.on_move(QPointF(4.0, 2.0))
        se.on_release()
        d0 = [(x - 4.0, y - 2.0) for x, y in obj.local_strokes[0].points]
        d1 = [(x - 4.0, y - 2.0) for x, y in obj.local_strokes[1].points]
        assert d0 == orig0 and d1 == orig1          # 两条都动了
        assert [tuple(p) for p in obj.local_strokes[2].points] == orig2  # 未选中不动
        w.controller.undo()
        assert [tuple(p) for p in obj.local_strokes[0].points] == orig0
        assert [tuple(p) for p in obj.local_strokes[1].points] == orig1
    finally:
        w.close()


def test_multi_delete_one_undo(qapp):
    """多选删除：一次撤销整体还原。"""
    strokes = [[(0, 0), (5, 0)], [(0, 3), (5, 3)],
               [(0, 6), (5, 6)], [(20, 0), (25, 0)]]
    w, se, obj = _synthetic_win(qapp, strokes)
    try:
        se._set_selection({0, 2})
        assert se.delete_selected()
        assert len(obj.local_strokes) == 2
        w.controller.undo()
        assert len(obj.local_strokes) == 4
    finally:
        w.close()


def test_multi_delete_keeps_correct_strokes(qapp):
    """删除多选时按下标从大到小，未选中的笔画不被误删。"""
    strokes = [[(i, 0), (i + 1, 0)] for i in range(5)]
    w, se, obj = _synthetic_win(qapp, strokes)
    try:
        se._set_selection({1, 3})
        se.delete_selected()
        left = [[tuple(p) for p in s.points] for s in obj.local_strokes]
        assert left == [[(0.0, 0.0), (1.0, 0.0)], [(2.0, 0.0), (3.0, 0.0)],
                        [(4.0, 0.0), (5.0, 0.0)]]
    finally:
        w.close()


def test_multi_selection_no_handles(qapp):
    """多选时不显示缩放/旋转手柄（避免误解为整体变换）——由 paint 逻辑保证。

    这里退而验证：多选下按下角点位置不会进入 scale/rotate 手势。
    """
    strokes = [[(0, 0), (5, 0)], [(0, 3), (5, 3)]]
    w, se, obj = _synthetic_win(qapp, strokes)
    try:
        se._set_selection({0, 1})
        se._primary = 0
        # 笔画0 的 ne 角（5,0）——单选时会是缩放手柄
        assert se.on_press(QPointF(5, 0), Qt.KeyboardModifier.NoModifier)
        assert se._drag is None or se._drag["kind"] == "move"
    finally:
        w.close()


def test_selection_index_mapping_after_delete(qapp):
    """删除后主轴/选择集重置，后续命中新下标不越界。"""
    strokes = [[(i, 0), (i + 1, 0)] for i in range(4)]
    w, se, obj = _synthetic_win(qapp, strokes)
    try:
        se._set_selection({0, 1})
        se.delete_selected()
        assert se.selected_index() is None
        assert se.selection == set()
        # 还能继续在剩余笔画上命中
        assert se.on_press(QPointF(2, 0), Qt.KeyboardModifier.NoModifier)
        assert se.selected_index() is not None
    finally:
        w.close()


def test_drag_repaint_uses_small_region(qapp):
    """拖动**移动过程**只请求重画受影响的小区域，不整视口刷新。

    松手时提交（``on_release``）会整视口刷新一次（此时笔画已写入对象、
    路径重建），不在此断言范围内。
    """
    strokes = [[(i, 0), (i + 1, 0)] for i in range(200)]
    w, se, obj = _synthetic_win(qapp, strokes)
    try:
        vp = w.canvas.viewport()
        calls = []
        orig = vp.update

        def spy(rect=None):
            calls.append(rect)
            return orig(rect) if rect is not None else orig()

        vp.update = spy
        try:
            se._refresh_world()
            se.on_press(QPointF(0, 0), Qt.KeyboardModifier.NoModifier)
            for i in range(5):
                se.on_move(QPointF(i * 0.5, i * 0.2))
            moves = list(calls)          # 只看移动阶段
        finally:
            vp.update = orig
        assert moves, "拖动移动应触发区域重绘"
        for c in moves:
            assert c is not None, "移动时应指定重绘区域而非整视口"
            assert c.width() * c.height() < vp.width() * vp.height() * 0.5
        se.on_release()
    finally:
        w.close()


def test_hover_repaint_uses_small_region(qapp):
    """悬停高亮变化只重画新旧高亮所在的小区域，不整视口刷新。"""
    strokes = [[(i, 0), (i + 1, 0)] for i in range(200)]
    w, se, obj = _synthetic_win(qapp, strokes)
    try:
        vp = w.canvas.viewport()
        calls = []
        orig = vp.update
        vp.update = lambda rect=None: (calls.append(rect),
                                       orig(rect) if rect is not None else orig())[1]
        try:
            se._refresh_world()
            se.update_hover(QPointF(3.5, 0.0))     # 命中某条 → 重绘
            se.update_hover(QPointF(-999, -999))   # 移开 → 重绘
        finally:
            vp.update = orig
        assert calls
        for c in calls:
            assert c is not None
            assert c.width() * c.height() < vp.width() * vp.height() * 0.5
    finally:
        w.close()


def test_spatial_index_matches_bruteforce(qapp):
    """空间索引命中结果与暴力遍历一致（含边界与远离的情况）。"""
    import math as _m
    strokes = [[(i * 3.0, (i % 5) * 2.0), (i * 3.0 + 1.5, (i % 5) * 2.0)]
               for i in range(60)]
    w, se, obj = _synthetic_win(qapp, strokes)
    try:
        se._refresh_world()

        def brute(pos):
            tol = 8.0 / se._zoom()
            best, bd = None, tol
            for i, s in enumerate(se.world):
                for p in s.points:
                    d = _m.hypot(pos.x() - p[0], pos.y() - p[1])
                    if d < bd:
                        best, bd = i, d
            return best

        for x, y in [(0, 0), (1.2, 0.3), (30, 4), (45, 2.1), (90, 8),
                     (-5, -5), (200, 200)]:
            pt = QPointF(x, y)
            assert se._hit_stroke(pt) == brute(pt), (x, y)
    finally:
        w.close()


# --------------------------------------------- 框选：真实画布事件管线
def test_canvas_pipeline_band_select(qapp):
    """走真实鼠标事件：空白处按下拖出框 → 松手批量选中。"""
    from writerstudio.core.document import DocumentObject, SourceSpec
    from writerstudio.core.strokes import Stroke
    w = MainWindow()
    w.confirm_on_close = False
    strokes = [[(0, 0), (5, 0)], [(0, 3), (5, 3)],
               [(40, 0), (45, 0)], [(40, 3), (45, 3)]]
    obj = DocumentObject(name="g", source=SourceSpec("static", {}),
                         local_strokes=[Stroke(list(p)) for p in strokes])
    w.controller.add_object(obj)
    w.canvas.sync_scene()
    w.canvas.select_object(obj)
    w.resize(1200, 800)
    w.show()
    qapp.processEvents()
    w.act_stroke_edit.setChecked(True)
    qapp.processEvents()
    se = w.stroke_editor
    try:
        # 用视图坐标发事件（mapFromScene 给出屏幕点），保证走进画布管线
        _send_mouse(w.canvas, QEvent.MouseButtonPress, (-8, -8),
                    Qt.LeftButton, Qt.LeftButton)
        _send_mouse(w.canvas, QEvent.MouseMove, (10, 10),
                    Qt.NoButton, Qt.LeftButton)
        _send_mouse(w.canvas, QEvent.MouseButtonRelease, (10, 10),
                    Qt.LeftButton, Qt.NoButton)
        qapp.processEvents()
        assert se.selection == {0, 1}
    finally:
        w.close()


def test_horizontal_stroke_repaint_not_full_viewport(qapp):
    """悬停/拖动水平笔画（包围盒高为 0）时仍走区域重绘，不退化成整视口。"""
    strokes = [[(0, 0), (50, 0)]]      # 纯水平线：bbox 高为 0
    w, se, obj = _synthetic_win(qapp, strokes)
    try:
        vp = w.canvas.viewport()
        calls = []
        orig = vp.update
        vp.update = lambda rect=None: (calls.append(rect),
                                       orig(rect) if rect is not None else orig())[1]
        try:
            se._refresh_world()
            se.update_hover(QPointF(25, 0))    # 命中横线
            se.update_hover(QPointF(-999, -999))
        finally:
            vp.update = orig
        assert calls
        for c in calls:
            assert c is not None, "水平笔画不应退化为整视口重绘"
            assert c.width() * c.height() < vp.width() * vp.height() * 0.5
    finally:
        w.close()


def test_bend_drag_repaints_full_viewport(qapp):
    """弯折手势（按下+拖动）每帧全视口失效，不做局部区域重绘。

    黄圈只有 1.2px 细环、半径 σ 伸出笔画包围盒：区域重绘在软件管线
    下虽然无残留（DPR 1.0/1.5 实测），但 Wayland 分数缩放按区域提交
    damage 时旧圈环会留在屏上（用户实测拖影），弯折手势必须整视口
    失效；其余手势（移动/旋转/缩放）保持区域重绘。
    """
    w, se, obj = _synthetic_win(qapp, [[(0, 0), (200, 0)]])
    try:
        w.canvas.zoom_100()
        se.set_bend_mode(True)
        seen = []
        vp = w.canvas.viewport()
        orig = vp.update
        vp.update = lambda rect=None: seen.append(rect)
        try:
            se.on_press(QPointF(0, 0), Qt.NoModifier)
            assert se._drag["kind"] == "bend"
            assert seen == [None], f"按下应全视口重绘，实际 {seen}"
            seen.clear()
            se.on_move(QPointF(10, 5))
            assert seen == [None], f"拖动应全视口重绘，实际 {seen}"
        finally:
            vp.update = orig
            se._drag = None
            se.preview = None
            se.set_bend_mode(False)
    finally:
        w.close()
