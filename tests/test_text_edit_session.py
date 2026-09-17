"""画布文字就地编辑会话 + 文字属性面板测试。

覆盖：会话增删/光标/选区、编辑写回对象与撤销合并、按键处理、
属性面板覆盖语义、会话选区 → 字符级字体覆盖、文本框宽度手柄链路。
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from writerstudio.fonts.builder import TextSpec, make_text_object  # noqa: E402
from writerstudio.ui.main_window import MainWindow  # noqa: E402
from writerstudio.ui.text_props_panel import TextPropsPanel  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


def _win(qapp):
    win = MainWindow()
    win.confirm_on_close = False
    return win


def _text_obj(win, text="abc", size=12.0, frame_width=0.0):
    spec = TextSpec(text=text, font_names=["futural"], size=size,
                    frame_width=frame_width)
    obj = make_text_object(spec, win.font_manager)
    win.controller.add_object(obj)
    win.canvas.sync_scene()
    win.canvas.select_object(obj)
    return obj


def _key(key, text="", mods=Qt.KeyboardModifier.NoModifier):
    return QKeyEvent(QEvent.KeyPress, key, mods, text)


# ------------------------------------------------------- 会话编辑写回
def test_session_insert_updates_object_and_merges_undo(qapp):
    win = _win(qapp)
    obj = _text_obj(win, "abc")
    win._start_text_edit(obj)
    ts = win._text_session
    assert ts is not None and ts.text == "abc"
    ts.insert_text("d")
    ts.insert_text("e")
    spec = TextSpec.from_data(obj.source.data)
    assert spec.text == "abcde"
    assert ts.caret == 5
    # 连续输入合并为一步撤销
    win.controller.undo()
    spec2 = TextSpec.from_data(obj.source.data)
    assert spec2.text == "abc"
    ts.finish()
    win.close()


def test_session_selection_and_delete(qapp):
    win = _win(qapp)
    obj = _text_obj(win, "hello")
    win._start_text_edit(obj)
    ts = win._text_session
    ts.anchor = 1
    ts.caret = 4                      # 选中 "ell"
    assert ts.has_selection()
    assert ts.selected_chars() == "ell"
    ts.insert_text("EY")              # 替换选区 → "hEYo"，光标在 o 前
    assert ts.text == "hEYo"
    assert ts.caret == 3
    ts.delete_backward()              # 删光标前的 "Y"
    assert ts.text == "hEo"
    assert ts.caret == 2
    ts.finish()
    win.close()


def test_session_backspace_delete_forward(qapp):
    win = _win(qapp)
    obj = _text_obj(win, "abcd")
    win._start_text_edit(obj)
    ts = win._text_session
    ts.handle_key(_key(Qt.Key_Backspace))     # 删 d
    assert ts.text == "abc"
    ts.handle_key(_key(Qt.Key_Left))
    ts.handle_key(_key(Qt.Key_Delete))        # 删 c
    assert ts.text == "ab"
    ts.finish()
    win.close()


def test_session_newline_and_undo_merge(qapp):
    win = _win(qapp)
    obj = _text_obj(win, "ab")
    win._start_text_edit(obj)
    ts = win._text_session
    ts.handle_key(_key(Qt.Key_Return))
    assert ts.text == "ab\n"
    n0 = win.controller.undo_stack.count()
    ts.insert_text("c")
    assert win.controller.undo_stack.count() == n0   # 合并不新增
    ts.finish()
    win.close()


def test_session_key_selection_extends(qapp):
    win = _win(qapp)
    obj = _text_obj(win, "abcdef")
    win._start_text_edit(obj)
    ts = win._text_session
    ts.handle_key(_key(Qt.Key_Left, "", Qt.ShiftModifier))
    ts.handle_key(_key(Qt.Key_Left, "", Qt.ShiftModifier))
    assert ts.selected_chars() == "ef"
    ts.handle_key(_key(Qt.Key_Left))          # 无 Shift → 清选区移动
    assert not ts.has_selection()
    assert ts.caret == 3
    ts.finish()
    win.close()


# ------------------------------------------------------- 命中测试/光标
def test_session_hit_test_maps_to_index(qapp):
    win = _win(qapp)
    obj = _text_obj(win, "HI", size=20.0)
    win._start_text_edit(obj)
    ts = win._text_session
    lay = obj.meta["layout"]
    c0 = lay.chars[0]
    # 第一个字符左缘 → 下标 0；末字右缘 → 下标 2
    p_left = obj.transform.apply((c0.origin[0] - 1.0, c0.origin[1]))
    p_end = obj.transform.apply(
        (lay.chars[-1].origin[0] + lay.chars[-1].advance + 1.0,
         lay.chars[-1].origin[1]))
    assert ts.hit_test(QPointF(*p_left)) == 0
    assert ts.hit_test(QPointF(*p_end)) == 2
    ts.finish()
    win.close()


def test_session_frame_hit_and_finish(qapp):
    win = _win(qapp)
    obj = _text_obj(win, "hello world " * 3, frame_width=0.0)
    win._start_text_edit(obj)
    ts = win._text_session
    assert ts.frame() is not None
    # 框内点击接管；框外点击不接管
    f = ts.frame()
    inside = obj.transform.apply((f[2] / 2.0, (f[1] + f[3]) / 2.0))
    outside = obj.transform.apply((f[2] + 500.0, f[1] + 500.0))
    assert ts.hit_frame(QPointF(*inside))
    assert not ts.hit_frame(QPointF(*outside))
    ts.finish()
    assert win._text_session is None
    win.close()


# ------------------------------------------------------- 框宽/属性面板
def test_frame_width_hook_relayouts(qapp):
    win = _win(qapp)
    obj = _text_obj(win, "word word word word word word ", frame_width=0.0)
    n0 = len(obj.meta["layout"].lines)
    win._on_text_frame_resizing(obj, 30.0)
    spec = TextSpec.from_data(obj.source.data)
    assert spec.frame_width == 30.0
    assert len(obj.meta["layout"].lines) > max(1, n0)   # 变窄 → 换行增多
    win.close()


def test_session_selection_drives_char_font_override(qapp):
    win = _win(qapp)
    obj = _text_obj(win, "abc")
    win._start_text_edit(obj)
    ts = win._text_session
    ts.anchor = 0
    ts.caret = 2                       # 选中 "ab"
    assert win._session_selected_chars() == "ab"
    win._sync_text_props()
    combo = win.text_props.font_combo
    other = next(i for i in range(combo.count())
                 if combo.itemData(i) not in (None, "", "futural"))
    combo.setCurrentIndex(other)
    spec = TextSpec.from_data(obj.source.data)
    # 字体只作用于选中字符；整对象链不变
    assert set(spec.char_overrides) == {"a", "b"}
    assert spec.char_overrides["a"]["font"] == combo.itemData(other)
    assert spec.font_names == ["futural"]
    ts.finish()
    win.close()


def test_text_props_panel_overwrite_fields(qapp):
    win = _win(qapp)
    obj = _text_obj(win, size=12.0)
    win._sync_text_props()
    assert not win.text_props.isHidden()      # 选中文本对象 → 显示
    win.text_props.size_spin.setValue(20.0)   # 即改即得
    spec = TextSpec.from_data(obj.source.data)
    assert spec.size == 20.0
    win.text_props.cs_spin.setValue(1.5)
    spec = TextSpec.from_data(obj.source.data)
    assert spec.char_spacing == 1.5
    win.text_props.fw_spin.setValue(60.0)
    spec = TextSpec.from_data(obj.source.data)
    assert spec.frame_width == 60.0
    win.close()


def test_text_props_hidden_without_text_selection(qapp):
    win = _win(qapp)
    _text_obj(win)
    win.canvas.scene().clearSelection()
    win.controller.notify_selection()
    assert win.text_props.isHidden()
    win.close()


def test_text_props_panel_roundtrip_without_signals(qapp):
    win = _win(qapp)
    fired = []
    panel = TextPropsPanel(win.font_manager)
    panel.propsChanged.connect(lambda: fired.append(1))
    spec = TextSpec(text="x", font_names=["futural"], size=18.0,
                    char_spacing=2.0, line_spacing=2.0)
    panel.set_from_spec(spec)
    v = panel.values()
    assert v["size"] == 18.0 and v["char_spacing"] == 2.0
    assert v["line_spacing"] == 2.0 and v["font"] == "futural"
    assert v["frame_width"] == 0.0
    assert fired == []                        # 回填不得触发变化信号
    win.close()


# ------------------------------------------------- 框手柄/双击属性入口
def _scene_mouse_event(etype, pos,
                       mods=Qt.KeyboardModifier.NoModifier):
    from PySide6.QtCore import QPointF
    from PySide6.QtWidgets import QGraphicsSceneMouseEvent
    ev = QGraphicsSceneMouseEvent(etype)
    ev.setButton(Qt.LeftButton)
    ev.setButtons(Qt.LeftButton)
    ev.setModifiers(mods)
    ev.setPos(QPointF(*pos))
    ev.setScenePos(QPointF(*pos))
    ev.setScreenPos(QPointF(*pos).toPoint())
    return ev


def test_text_handle_drag_reflows_not_stretches(qapp):
    """拖文本对象的右缘手柄 = 改框宽重新换行，变换不变（不拉伸）。"""
    win = _win(qapp)
    obj = _text_obj(win, "hello world hello world hello world", size=10.0)
    win.canvas.select_object(obj)
    win.canvas.zoom_100()
    item = win.canvas._items[obj.id]
    from writerstudio.ui.items import Handle
    t0 = obj.transform
    r = item._frame_rect_or_extent()
    n_lines0 = len(obj.meta["layout"].lines)
    press = item._handle_positions()[Handle.R]
    item.mousePressEvent(_scene_mouse_event(QEvent.GraphicsSceneMousePress,
                                            (press.x(), press.y())))
    assert item._drag_kind == "frame"
    item.mouseMoveEvent(_scene_mouse_event(QEvent.GraphicsSceneMouseMove,
                                           (r.right() - 25.0, r.center().y())))
    item.mouseReleaseEvent(_scene_mouse_event(QEvent.GraphicsSceneMouseRelease,
                                              (r.right() - 25.0, r.center().y())))
    spec = TextSpec.from_data(obj.source.data)
    assert spec.frame_width == pytest.approx(r.width() - 25.0, abs=0.3)
    assert obj.transform == t0                 # 变换没变 → 字形没被拉伸
    assert len(obj.meta["layout"].lines) > n_lines0   # 变窄 → 换行增多
    win.controller.undo()
    assert TextSpec.from_data(obj.source.data).frame_width == 0.0
    assert len(obj.meta["layout"].lines) == n_lines0
    win.close()


def test_text_left_handle_keeps_right_edge_fixed(qapp):
    """拖左缘手柄：框宽变窄、对象平移（右缘钉住），宏内一步撤销。"""
    win = _win(qapp)
    obj = _text_obj(win, "hello world hello world", size=10.0)
    win.canvas.select_object(obj)
    win.canvas.zoom_100()
    item = win.canvas._items[obj.id]
    from writerstudio.ui.items import Handle
    r = item._frame_rect_or_extent()
    origin0 = obj.transform.apply((0.0, 0.0))
    press = item._handle_positions()[Handle.L]
    item.mousePressEvent(_scene_mouse_event(QEvent.GraphicsSceneMousePress,
                                            (press.x(), press.y())))
    item.mouseMoveEvent(_scene_mouse_event(QEvent.GraphicsSceneMouseMove,
                                           (r.left() + 10.0, r.center().y())))
    item.mouseReleaseEvent(_scene_mouse_event(QEvent.GraphicsSceneMouseRelease,
                                              (r.left() + 10.0, r.center().y())))
    spec = TextSpec.from_data(obj.source.data)
    assert spec.frame_width == pytest.approx(r.width() - 10.0, abs=0.3)
    origin1 = obj.transform.apply((0.0, 0.0))
    assert origin1[0] - origin0[0] == pytest.approx(10.0, abs=0.3)  # 右缘不动
    # 宏：一次撤销同时还原宽度与位移
    win.controller.undo()
    assert TextSpec.from_data(obj.source.data).frame_width == 0.0
    assert obj.transform.apply((0.0, 0.0)) == pytest.approx(origin0)
    win.close()


def test_text_ctrl_handle_drag_scales_instead(qapp):
    """Ctrl + 拖角手柄 → 保留旧的缩放变换行为（不改动框宽）。"""
    win = _win(qapp)
    obj = _text_obj(win, "hi", size=10.0)
    win.canvas.select_object(obj)
    win.canvas.zoom_100()
    item = win.canvas._items[obj.id]
    from writerstudio.ui.items import Handle
    ctrl_mods = Qt.ControlModifier
    r = item._frame_rect_or_extent()
    br = item._handle_positions()[Handle.BR]
    item.mousePressEvent(_scene_mouse_event(QEvent.GraphicsSceneMousePress,
                                            (br.x(), br.y()), ctrl_mods))
    assert item._drag_kind == "handle"          # 走缩放而不是改框宽
    ev = _scene_mouse_event(QEvent.GraphicsSceneMouseMove,
                            (r.right() * 2.0, r.bottom() * 2.0), ctrl_mods)
    item._preview(item._compute_handle_transform(ev))
    item.mouseReleaseEvent(ev)
    assert TextSpec.from_data(obj.source.data).frame_width == 0.0
    assert obj.transform != __import__(
        "writerstudio.core.geometry", fromlist=["AffineTransform"]
    ).AffineTransform.identity()
    win.close()


def test_double_click_text_opens_properties_dialog(qapp, monkeypatch):
    win = _win(qapp)
    obj = _text_obj(win, "abc")
    opened = []

    class FakeDlg:
        Accepted = 1

        def __init__(self, spec, manager, parent=None):
            opened.append(spec)

        def exec(self):
            return 0

        def result_spec(self):
            return TextSpec(text="abc", font_names=["futural"])

    import writerstudio.ui.main_window as mw
    monkeypatch.setattr(mw, "TextEditDialog", FakeDlg)
    win._on_object_activated(obj)       # 双击文本 → 属性对话框
    assert len(opened) == 1
    assert opened[0].text == "abc"
    win.close()


def test_double_click_while_editing_finishes_session(qapp, monkeypatch):
    win = _win(qapp)
    obj = _text_obj(win, "abc")

    class FakeDlg:
        Accepted = 1

        def __init__(self, spec, manager, parent=None):
            pass

        def exec(self):
            return 0

        def result_spec(self):
            return TextSpec(text="abc", font_names=["futural"])

    import writerstudio.ui.main_window as mw
    monkeypatch.setattr(mw, "TextEditDialog", FakeDlg)
    win._start_text_edit(obj)
    assert win._text_session is not None
    win._on_object_activated(obj)       # 编辑中双击 → 结束会话再开对话框
    assert win._text_session is None
    win.close()


def test_click_on_selected_text_starts_editing(qapp):
    win = _win(qapp)
    obj = _text_obj(win, "hi")
    item = win.canvas._items[obj.id]
    win.canvas.zoom_100()          # 让手柄命中容差接近真实使用比例
    # 命中文本框内部的一点（本地坐标 = 场景坐标，未变换）
    lay = obj.meta["layout"]
    p0 = (lay.chars[0].origin[0] + 4.0, lay.chars[0].origin[1] + 2.0)
    win.canvas.scene().clearSelection()
    # 第一次点击：仅选中
    item.mousePressEvent(_scene_mouse_event(QEvent.GraphicsSceneMousePress, p0))
    item.mouseReleaseEvent(_scene_mouse_event(QEvent.GraphicsSceneMouseRelease, p0))
    assert item.isSelected()
    assert win._text_session is None
    # 第二次点击（对象已选中、无拖动）：进入就地编辑
    item.mousePressEvent(_scene_mouse_event(QEvent.GraphicsSceneMousePress, p0))
    item.mouseReleaseEvent(_scene_mouse_event(QEvent.GraphicsSceneMouseRelease, p0))
    assert win._text_session is not None
    assert win._text_session.obj is obj
    win._text_session.finish()
    win.close()


def test_click_drag_on_selected_text_does_not_edit(qapp):
    win = _win(qapp)
    obj = _text_obj(win, "hi")
    item = win.canvas._items[obj.id]
    win.canvas.zoom_100()
    lay = obj.meta["layout"]
    p0 = (lay.chars[0].origin[0] + 4.0, lay.chars[0].origin[1] + 2.0)
    win.canvas.scene().clearSelection()
    item.mousePressEvent(_scene_mouse_event(QEvent.GraphicsSceneMousePress, p0))
    item.mouseReleaseEvent(_scene_mouse_event(QEvent.GraphicsSceneMouseRelease, p0))
    item.mousePressEvent(_scene_mouse_event(QEvent.GraphicsSceneMousePress, p0))
    # 拖动了 5mm 再松手 → 是移动，不是编辑
    item.mouseMoveEvent(_scene_mouse_event(QEvent.GraphicsSceneMouseMove,
                                           (p0[0] + 5.0, p0[1])))
    item.mouseReleaseEvent(_scene_mouse_event(QEvent.GraphicsSceneMouseRelease,
                                              (p0[0] + 5.0, p0[1])))
    assert win._text_session is None
    win.close()
