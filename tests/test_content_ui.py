"""P4 UI 冒烟测试：富内容对话框与插入流程。"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from writerstudio.content.builder import SOURCE_EQUATION, SOURCE_MARKDOWN  # noqa: E402
from writerstudio.ui.content_dialog import (  # noqa: E402
    KIND_EQUATION,
    KIND_MARKDOWN,
    KIND_SVG,
    ContentDialog,
)
from writerstudio.ui.main_window import MainWindow  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


def test_content_dialog_builds_all_pages(qapp):
    win = MainWindow()
    dlg = ContentDialog(win.font_manager, initial_kind=KIND_MARKDOWN)
    dlg.resize(700, 560)
    dlg.show()
    qapp.processEvents()
    assert dlg.stack.count() == 5   # Markdown/公式/LaTeX/TikZ/SVG
    # Markdown 预览应产出信息
    assert "笔画" in dlg.preview_label.text()
    dlg.close()
    win.close()


def test_content_dialog_equation_payload(qapp):
    win = MainWindow()
    dlg = ContentDialog(win.font_manager, initial_kind=KIND_EQUATION)
    dlg.eq_edit.setPlainText(r"\frac{a}{b}")
    dlg._update_preview()
    p = dlg.payload()
    assert p.kind == KIND_EQUATION
    assert p.data["latex"] == r"\frac{a}{b}"
    dlg.close()
    win.close()


def test_content_dialog_switch_kind(qapp):
    win = MainWindow()
    dlg = ContentDialog(win.font_manager, initial_kind=KIND_MARKDOWN)
    dlg.kind_combo.setCurrentIndex(1)
    qapp.processEvents()
    assert dlg.stack.currentIndex() == 1
    dlg.kind_combo.setCurrentIndex(3)
    qapp.processEvents()
    assert dlg.stack.currentIndex() == 3
    dlg.close()
    win.close()


def test_main_window_insert_markdown(qapp, monkeypatch):
    win = MainWindow()
    n0 = len(win.controller.doc)

    def fake_exec(self):
        return ContentDialog.Accepted

    monkeypatch.setattr(ContentDialog, "exec", fake_exec)
    win._insert_content(KIND_MARKDOWN)
    assert len(win.controller.doc) == n0 + 1
    obj = win.controller.doc.objects[-1]
    assert obj.source.kind == SOURCE_MARKDOWN
    assert obj.local_strokes
    win.close()


def test_main_window_insert_equation(qapp, monkeypatch):
    win = MainWindow()
    n0 = len(win.controller.doc)

    def fake_exec(self):
        self.eq_edit.setPlainText("E=mc^2")
        return ContentDialog.Accepted

    monkeypatch.setattr(ContentDialog, "exec", fake_exec)
    win._insert_content(KIND_EQUATION)
    assert len(win.controller.doc) == n0 + 1
    obj = win.controller.doc.objects[-1]
    assert obj.source.kind == SOURCE_EQUATION
    assert obj.local_strokes
    win.close()


@pytest.mark.skipif(
    not __import__("writerstudio.content.svg_import",
                   fromlist=["svg_available"]).svg_available(),
    reason="svgelements 未安装")
def test_main_window_insert_svg(qapp, monkeypatch, tmp_path):
    win = MainWindow()
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="50mm" height="30mm" '
           'viewBox="0 0 50 30"><rect x="2" y="2" width="20" height="15" '
           'fill="none" stroke="#000"/></svg>')
    p = tmp_path / "a.svg"
    p.write_text(svg)
    n0 = len(win.controller.doc)

    def fake_exec(self):
        self.svg_path.setText(str(p))
        return ContentDialog.Accepted

    monkeypatch.setattr(ContentDialog, "exec", fake_exec)
    win._insert_content(KIND_SVG)
    assert len(win.controller.doc) == n0 + 1
    obj = win.controller.doc.objects[-1]
    assert obj.local_strokes
    win.close()


@pytest.mark.skipif(
    not __import__("writerstudio.content.tikz",
                   fromlist=["tikz_available"]).tikz_available(),
    reason="TikZ 工具链不可用")
def test_main_window_inserts_tikz_object(qapp):
    """走主窗口的富内容构建路径插入 TikZ，并可施加手绘扰动。"""
    from writerstudio.ui.content_dialog import ContentPayload, KIND_TIKZ
    from writerstudio.perturb.apply import apply_perturb
    from writerstudio.perturb.params import PerturbParams
    win = MainWindow()
    payload = ContentPayload(KIND_TIKZ, {
        "source": r"\draw (0,0)--(3,2)--(0,2)--cycle;", "target_width": 50.0})
    obj = win._build_content_object(payload)
    assert obj is not None and obj.source.kind == "tikz"
    assert obj.local_strokes
    base = [s.points for s in obj.local_strokes]
    apply_perturb(obj, PerturbParams.vector_hand_drawn(40.0), win.font_manager)
    assert [s.points for s in obj.local_strokes] != base
    win.controller.add_object(obj)
    win.canvas.sync_scene()
    assert len(win.controller.doc) == 1
    win.close()


def test_stroke_delete_on_content_object_survives_perturb(qapp):
    """画布删除内容对象(公式)的一条笔画后，调扰动参数不得把它加回来。"""
    from writerstudio.content.builder import make_equation_object
    from writerstudio.perturb.apply import apply_perturb
    from writerstudio.perturb.params import PerturbParams
    win = MainWindow()
    obj = make_equation_object(r"a+b=c", 8.0)
    win.controller.add_object(obj)
    win.canvas.select_object(obj)
    n0 = len(obj.local_strokes)
    assert n0 >= 2
    win._delete_stroke(obj, 0)
    assert len(obj.local_strokes) == n0 - 1
    for seed in (1, 2, 3):
        p = PerturbParams.natural(8.0)
        p.seed = seed
        apply_perturb(obj, p, win.font_manager)
        assert len(obj.local_strokes) == n0 - 1
    win.close()


def test_stroke_modify_on_content_object_survives_perturb(qapp):
    """改动内容对象的笔画后，调扰动参数不得让改动回退。"""
    from writerstudio.content.builder import make_equation_object
    from writerstudio.core.strokes import Stroke
    from writerstudio.perturb.apply import apply_perturb
    from writerstudio.perturb.params import PerturbParams
    win = MainWindow()
    obj = make_equation_object(r"a+b=c", 8.0)
    win.controller.add_object(obj)
    win.canvas.select_object(obj)
    s = obj.local_strokes[0]
    new = Stroke([(x + 4.0, y - 2.0) for x, y in s.points], s.closed)
    win._commit_stroke_edit(obj, 0, new)
    want = obj.local_strokes[0].points
    p = PerturbParams.natural(8.0)
    p.seed = 77
    apply_perturb(obj, p, win.font_manager)
    assert obj.local_strokes[0].points == want
    win.close()


# ================================================ 表格宽度（与文字一致）
def _md_table_obj(win, text=None):
    from writerstudio.content.builder import make_markdown_object
    src = text or ("| 名称 | 说明 |\n|:--|:--|\n"
                   "| 项目 | 一段比较长的说明文字内容 |\n")
    obj = make_markdown_object(src, win.font_manager)
    win.controller.add_object(obj)
    win.canvas.sync_scene()
    return obj


def test_markdown_object_records_table_box(qapp):
    """构建 Markdown 表格对象后记录表格外框（供画布显示宽度手柄）。"""
    win = MainWindow()
    obj = _md_table_obj(win)
    box = obj.meta.get("md_table_box")
    assert box is not None
    x0, y_top, x1, y_bottom = box
    assert x1 > x0 and y_top > y_bottom
    win.close()


def test_content_dialog_table_jitter_roundtrip(qapp):
    """表格随机化旋钮 → payload → 对象样式 → 回填对话框，全链路一致。"""
    win = MainWindow()
    dlg = ContentDialog(win.font_manager, initial_kind=KIND_MARKDOWN)
    dlg.md_tab_ej.setValue(0.9)
    dlg.md_tab_os.setValue(1.1)
    dlg.md_tab_rj.setValue(0.7)
    dlg.md_tab_cj.setValue(0.6)
    payload = dlg.payload()
    for key, val in (("table_end_jitter", 0.9), ("table_overshoot", 1.1),
                     ("table_row_jitter", 0.7), ("table_col_jitter", 0.6)):
        assert payload.data[key] == pytest.approx(val)
    # 构建对象：样式进 source.data["style"]（可持久化/重排）
    obj = win._build_content_object(payload)
    st = obj.source.data["style"]
    assert st["table_end_jitter"] == pytest.approx(0.9)
    assert st["table_overshoot"] == pytest.approx(1.1)
    assert st["table_row_jitter"] == pytest.approx(0.7)
    assert st["table_col_jitter"] == pytest.approx(0.6)
    # 回填：编辑已有对象时旋钮恢复其样式值
    dlg2 = ContentDialog(win.font_manager, initial_kind=KIND_MARKDOWN)
    dlg2.load_from_object(obj)
    assert dlg2.md_tab_ej.value() == pytest.approx(0.9)
    assert dlg2.md_tab_os.value() == pytest.approx(1.1)
    assert dlg2.md_tab_rj.value() == pytest.approx(0.7)
    assert dlg2.md_tab_cj.value() == pytest.approx(0.6)
    dlg.close()
    dlg2.close()
    win.close()


def test_table_reflow_keeps_jitter_stable(qapp):
    """拖宽度重排：随机化参数保留，同宽度重排结果确定（不跳变）。"""
    win = MainWindow()
    obj = _md_table_obj(win)
    st = obj.source.data["style"]
    st["table_row_jitter"] = 0.8
    st["table_col_jitter"] = 0.8
    win._reflow_table(obj, 60.0)
    st2 = obj.source.data["style"]
    assert st2["table_row_jitter"] == pytest.approx(0.8)
    assert st2["table_col_jitter"] == pytest.approx(0.8)
    # 同宽度再排一次：随机种子不变 → 笔画一致
    p2 = [s.points for s in obj.local_strokes]
    win._reflow_table(obj, 60.0)
    p3 = [s.points for s in obj.local_strokes]
    assert p2 == p3
    win.close()


def test_markdown_item_shows_table_width_handles(qapp):
    """Markdown 表格对象显示左右缘宽度手柄（与文本框同一套交互）。"""
    from writerstudio.ui.items import Handle
    win = MainWindow()
    obj = _md_table_obj(win)
    item = win.canvas._items[obj.id]
    handles = item._visible_handles()
    assert Handle.L in handles and Handle.R in handles
    win.close()


def test_table_frame_resizing_relayouts(qapp):
    """拖表格宽度手柄：写回 style.table_width 并按新宽度重排。
    首个事件直接套用（限流不吞掉即时反馈）。"""
    win = MainWindow()
    obj = _md_table_obj(win)
    h0 = obj.meta["md_table_box"][1] - obj.meta["md_table_box"][3]
    win._on_table_frame_resizing(obj, 25.0)
    st = (obj.source.data.get("style") or {})
    assert st.get("table_width") == 25.0
    box = obj.meta.get("md_table_box")
    assert box[2] == pytest.approx(25.0, abs=1.0)   # 表格宽贴合手柄值
    h1 = box[1] - box[3]
    assert h1 > h0                                  # 折行 → 更高
    win.close()


def test_table_frame_resize_flush_is_idempotent(qapp):
    """松手 flush 后限流状态清空，撤销快照拿到最终宽度。"""
    win = MainWindow()
    obj = _md_table_obj(win)
    win._flush_frame_resize()          # 无拖动时应安全（no-op）
    win._on_table_frame_resizing(obj, 40.0)
    win._flush_frame_resize()
    assert win._frame_ref is None
    assert (obj.source.data.get("style") or {}).get("table_width") == 40.0
    win.close()


def test_edit_text_action_works_for_markdown(qapp, monkeypatch):
    """「编辑文本」不再只对纯文本生效：Markdown 对象也打开对应编辑器。"""
    win = MainWindow()
    obj = _md_table_obj(win)
    win.canvas.select_object(obj)

    opened = {}

    def fake_edit_object(o):
        opened["obj"] = o

    monkeypatch.setattr(win, "_edit_object", fake_edit_object)
    win._edit_text()
    assert opened.get("obj") is obj
    win.close()


def test_edit_text_action_prompts_on_empty_selection(qapp):
    """没有选中对象时「编辑文本」给出提示而非静默/报纯文本错误。"""
    win = MainWindow()
    win.canvas.clear_selection()
    win._edit_text()          # 不应抛异常
    win.close()


# ================================================ 拖宽性能（定向同步/延迟命中区）
def test_frame_drag_defers_hit_path_and_rebuilds_on_release(qapp):
    """拖宽手柄期间跳过命中区描边（大文档高频重排的热点），松手补建。"""
    win = MainWindow()
    obj = _md_table_obj(win)
    item = win.canvas._items[obj.id]
    # 模拟进入拖宽：拖动中的路径重建不产生命中区
    item._drag_kind = 'frame'
    try:
        win._on_table_frame_resizing(obj, 60.0)     # 立即套用一次
        qapp.processEvents()
        assert item._hit_deferred is True
        assert item._hit_path.isEmpty()
    finally:
        item._drag_kind = None
        win._flush_frame_resize()
    # 松手补建：命中区非空且跟随最终笔画
    assert item._hit_deferred is False
    assert not item._hit_path.isEmpty()
    win.close()


def test_reflow_text_syncs_single_object_not_full_scene(qapp, monkeypatch):
    """拖动重排只同步目标对象（不把参考图等无关 item 标脏触发整视口重绘）。"""
    from writerstudio.fonts.builder import TextSpec, make_text_object
    win = MainWindow()
    spec = TextSpec(text="word word word word word ", font_names=["futural"],
                    size=12.0, frame_width=0.0)
    tobj = make_text_object(spec, win.font_manager)
    win.controller.add_object(tobj)
    win.canvas.sync_scene()
    calls = []
    monkeypatch.setattr(win.canvas, "sync_scene",
                        lambda: calls.append("full"))
    win._reflow_text(tobj, 30.0)
    assert calls == [], "拖动重排不应触发全场景同步"
    item = win.canvas._items[tobj.id]
    assert item._path_rev == tobj.strokes_rev     # 目标对象已同步
    win.close()
