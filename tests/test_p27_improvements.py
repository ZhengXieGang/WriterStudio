"""P27 改进回归测试：

    * ① 内置字体可移除（= 隐藏，可恢复；用户字体仍删文件）
    * ② 对象列表双击打开对象编辑界面
    * ③④⑤ 界面文案：无 σ、无 $X/$H、无参考层灰字
    * ⑥ 保存/恢复撤销历史（重开文件可回撤）+ 起点随项目保存
    * ⑦ 起点标记双态按钮与默认隐藏
"""

from __future__ import annotations

import json
import os
import re

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from writerstudio.core.document import Document, make_static_object  # noqa: E402
from writerstudio.core.sample import make_rect  # noqa: E402
from writerstudio.project import load_project, save_project  # noqa: E402
from writerstudio.ui.controller import JOURNAL_LIMIT  # noqa: E402
from writerstudio.ui.main_window import MainWindow  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


def _win(qapp):
    from writerstudio.settings import Settings
    win = MainWindow(settings=Settings())
    win.confirm_on_close = False
    return win


# ==================================================== ① 内置字体可移除
def test_builtin_font_remove_hides_and_restores(qapp, monkeypatch):
    """内置字体「移除」= 记住隐藏（文件保留），可在「已隐藏…」恢复。"""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QMessageBox

    win = _win(qapp)
    fp = win.font_panel
    builtin = next((e for e in fp.manager.entries()
                    if "builtin" in str(e.path)), None)
    if builtin is None:
        pytest.skip("无内置字体")
    assert builtin.path.exists()
    monkeypatch.setattr(
        "writerstudio.ui.font_panel.QMessageBox.question",
        lambda *a, **k: QMessageBox.Yes)
    fp.manager.hide(builtin.name, False)
    fp.refresh()
    fp.list.setCurrentRow(
        next(i for i in range(fp.list.count())
             if fp.list.item(i).data(Qt.UserRole) == builtin.name))
    fp._remove()
    assert fp.manager.is_hidden(builtin.name)
    assert builtin.path.exists()          # 文件不删
    # 「已隐藏…」恢复路径
    fp.manager.hide(builtin.name, False)
    assert not fp.manager.is_hidden(builtin.name)
    win.close()


def test_user_font_remove_deletes_copy(qapp, monkeypatch, tmp_path):
    """用户字体「移除」仍删除字库中的副本并注销条目。"""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QMessageBox

    win = _win(qapp)
    fp = win.font_panel
    src = tmp_path / "mystroke.json"
    src.write_text(json.dumps({"A": [[[0, 0], [1, 1]]]}), encoding="utf-8")
    try:
        entry = fp.manager.import_font(src, kind="stroke-json")
    except Exception:
        pytest.skip("stroke-json 导入失败")
    copy_path = entry.path                # 字库中的副本
    monkeypatch.setattr(
        "writerstudio.ui.font_panel.QMessageBox.question",
        lambda *a, **k: QMessageBox.Yes)
    fp.refresh()
    fp.list.setCurrentRow(
        next(i for i in range(fp.list.count())
             if fp.list.item(i).data(Qt.UserRole) == entry.name))
    fp._remove()
    assert fp.manager.get_entry(entry.name) is None
    assert not copy_path.exists()         # 副本删除；原文件 src 保留
    assert src.exists()
    win.close()


# ==================================================== ② 对象列表双击编辑
def test_object_list_double_click_opens_editor(qapp, monkeypatch):
    win = _win(qapp)
    doc = Document()
    obj = make_static_object([make_rect(0, 0, 10, 10)])
    doc.add(obj)
    win.controller.set_document(doc)
    win._refresh_object_panel()
    called = []
    monkeypatch.setattr(win, "_edit_object", lambda o: called.append(o))
    win.obj_list.itemDoubleClicked.emit(win.obj_list.item(0))
    assert called and called[0] is obj
    win.close()


# ==================================================== ③④⑤ 文案清理
def test_no_sigma_visible_labels(qapp):
    """面板标签不再含 σ 字符。"""
    from PySide6.QtWidgets import QLabel

    win = _win(qapp)
    texts = [w.text() for w in win.global_perturb.findChildren(QLabel)]
    assert not any("σ" in t for t in texts), [t for t in texts if "σ" in t]
    win.close()


def test_home_unlock_buttons_plain_text(qapp):
    win = _win(qapp)
    assert win.machine_panel.home_btn.text() == "回零"
    assert win.machine_panel.unlock_btn.text() == "解锁"
    win.close()


def test_reference_panel_no_grey_hint(qapp):
    win = _win(qapp)
    from PySide6.QtWidgets import QLabel
    texts = [w.text() for w in win.reference_panel.findChildren(QLabel)]
    assert not any("仅用于对齐" in t for t in texts)
    win.close()


# ==================================================== ⑥ 撤销历史持久化
def test_project_roundtrip_with_history(qapp, tmp_path):
    win = _win(qapp)
    doc = Document()
    doc.add(make_static_object([make_rect(0, 0, 10, 10)]))
    win.controller.set_document(doc)
    # 两步可撤销操作：移动 + 删除
    obj = win.controller.doc.objects[0]
    from writerstudio.core.geometry import AffineTransform
    win.controller.set_transform(obj, AffineTransform.translate(5, 0), "移动")
    win.controller.undo()                     # 撤销移动（产生 redo 分支）
    win.controller.set_transform(obj, AffineTransform.translate(0, 5), "下移")

    path = tmp_path / "p.wsproj"
    save_project(path, win._collect_project())
    data = json.loads(path.read_text(encoding="utf-8"))
    # 新版为池化格式：对象抽进 history_pool 去重，条目按池下标引用
    assert "history_refs" in data and len(data["history_refs"]) >= 1
    assert "history_pool" in data and data["history_pool"]
    assert all("objects" in e and "text" in e for e in data["history_refs"])

    # 重新打开：历史恢复、可逐步回撤
    win2 = _win(qapp)
    from writerstudio.project import load_project
    project = load_project(path)
    win2._apply_project(project)
    assert win2.controller.can_undo
    win2.controller.undo()
    obj2 = win2.controller.doc.objects[0]
    # 撤销「下移」：Y 平移回到 0（dx,dy = e,f）
    assert obj2.transform.e == pytest.approx(0.0)
    assert obj2.transform.f == pytest.approx(0.0)
    win.close()
    win2.close()


def test_history_journal_capped(qapp):
    """快照日志有上限，长会话不无限膨胀。"""
    win = _win(qapp)
    doc = Document()
    doc.add(make_static_object([make_rect(0, 0, 10, 10)]))
    win.controller.set_document(doc)
    from writerstudio.core.geometry import AffineTransform
    obj = win.controller.doc.objects[0]
    for i in range(JOURNAL_LIMIT + 10):
        win.controller.set_transform(obj, AffineTransform.translate(0.01, 0),
                                     f"移动 {i}")
    assert len(win.controller.undo_stack.journal) <= JOURNAL_LIMIT
    win.close()


def test_stroke_edits_survive_roundtrip(qapp, tmp_path):
    """保存文件时保留手工笔画修改（stroke_edits 编辑层在 source.data 里）。"""
    from writerstudio.fonts.builder import TextSpec, make_text_object
    win = _win(qapp)
    doc = Document()
    obj = make_text_object(TextSpec(text="A", font_names=["futural"]),
                           win.font_manager)
    doc.add(obj)
    win.controller.set_document(doc)
    n0 = len(obj.local_strokes)
    if n0 < 1:
        pytest.skip("字体不可用")
    win._delete_stroke(obj, 0)
    assert len(obj.source.data.get("stroke_edits", {}).get("deleted", [])) == 1

    path = tmp_path / "p.wsproj"
    save_project(path, win._collect_project())
    project = load_project(path)
    obj2 = project.document.objects[0]
    assert obj2.source.data.get("stroke_edits", {}).get("deleted") == [0]
    assert len(obj2.local_strokes) == n0 - 1
    win.close()


def test_start_point_saved_with_project(qapp, tmp_path):
    win = _win(qapp)
    win.machine_panel.set_start_point("canvas", 33.0, 44.0)
    path = tmp_path / "p.wsproj"
    save_project(path, win._collect_project())
    project = load_project(path)
    assert project.start_point.pen_marker == pytest.approx((33.0, 44.0))
    win.close()


# ==================================================== ⑦ 起点标记双态
def test_marker_hidden_by_default(qapp):
    """默认不显示起点标记。"""
    win = _win(qapp)
    assert win.machine_panel.start_point.pen_marker is None
    assert win.canvas.pen_marker() is None
    win.close()


def test_mark_start_button_toggles_to_remove(qapp):
    win = _win(qapp)
    mp = win.machine_panel
    assert mp.mark_start_btn.text() == "手动标记起点"
    win._on_start_marked(20.0, 30.0)
    assert mp.mark_start_btn.text() == "移除手动标记点"
    # 再点 = 移除
    mp._on_mark_start_clicked()
    assert mp.start_point.pen_marker is None
    assert mp.mark_start_btn.text() == "手动标记起点"
    win.close()


def test_start_summary_mentions_unset(qapp):
    win = _win(qapp)
    assert "未设置" in win.machine_panel.start_summary.text()
    win.close()


# ==================================================== 坐标轴对调/反转
def test_axis_mapping_roundtrip_all_combos():
    """固定映射 + 轴选项：坐标往返换算一致，机械零点恒对应纸张左上角。"""
    import itertools

    from writerstudio.machine.config import machine_to_page, page_to_machine
    h = 210.0
    p0 = (37.5, 88.25)
    for swap, ix, iy in itertools.product((False, True), repeat=3):
        m = page_to_machine(p0, h, swap, ix, iy)
        assert machine_to_page(m, h, swap, ix, iy) == pytest.approx(p0)
        # 机械零点 → 纸张左上角（轴修正是线性的，零点不动）
        assert machine_to_page((0.0, 0.0), h, swap, ix, iy) \
            == pytest.approx((0.0, h))
    # 无选项时正向自逆（旧行为不变）
    assert page_to_machine(p0, h) == pytest.approx((37.5, h - 88.25))
    assert page_to_machine(page_to_machine(p0, h), h) == pytest.approx(p0)


def test_axis_options_applied_to_gcode(tmp_path):
    """对调/反转勾选后 G-code 坐标随之变换。"""
    from writerstudio.machine.config import GCodeConfig
    from writerstudio.machine.gcode_gen import generate_gcode
    from writerstudio.core.strokes import Stroke

    page_h = 210.0
    stroke = Stroke([(0.0, 0.0), (10.0, 10.0)])

    cfg = GCodeConfig(swap_xy=True, page_size=(297.0, page_h))
    text = "\n".join(generate_gcode([stroke], cfg, optimize=False).lines)
    # 基础映射后 (0,210)…(10,200)，对调 → x∈[200,210]、y∈[0,10]
    assert "X210 Y0" in text
    assert "X200 Y10" in text

    cfg = GCodeConfig(invert_y=True, page_size=(297.0, page_h))
    text = "\n".join(generate_gcode([stroke], cfg, optimize=False).lines)
    assert "Y-210" in text
    assert "Y-200" in text

    cfg = GCodeConfig(invert_x=True, page_size=(297.0, page_h))
    text = "\n".join(generate_gcode([stroke], cfg, optimize=False).lines)
    # −0 归一为 0（不出 X-0）；起点 (0,210) → (0,210)，(10,10) → (−10,200)。
    # 按 token 断言：默认斜抬的补点会产生 X-0.354 这类合法坐标，
    # 裸子串 "X-0" 会误伤；负零的形态是 "X-0" 后跟空格或行尾
    assert not re.search(r"X-0(?![.\d])", text)
    assert "X0 Y210" in text
    assert "X-10 Y200" in text


def test_axis_checkboxes_wired(qapp):

    win = _win(qapp)
    mp = win.machine_panel
    assert mp.swap_xy_check.text() == "对调 X/Y 轴"
    # 勾选 → 配置；回填 → 勾选
    mp.swap_xy_check.setChecked(True)
    mp.invert_y_check.setChecked(True)
    cfg = mp.current_config()
    assert cfg.swap_xy and cfg.invert_y and not cfg.invert_x
    mp.config = cfg.clone()
    mp._apply_config_to_ui()
    assert mp.swap_xy_check.isChecked() and mp.invert_y_check.isChecked()
    assert not mp.invert_x_check.isChecked()
    # 回填不触发 axisOptionsChanged（blockSignals）
    fired = []
    mp.axisOptionsChanged.connect(lambda: fired.append(1))
    mp._apply_config_to_ui()
    assert not fired
    win.close()


def test_axis_options_update_origin_arrows_and_marker(qapp):
    """对调 X/Y：机械原点圆点钉在纸角不动，X/Y 箭头互换指向；
    起点标记「钉在纸面」——画布（纸面）位置不变，机器坐标按新轴映射
    重算（P42：轴变化后标记显示跳走会让校准失效、写出来错位）。"""
    win = _win(qapp)
    page = win.controller.doc.page
    h = page.height
    win.machine_panel.start_point.pen_marker = (50.0, 60.0)
    win._on_machine_config_changed()
    pos0, xd0, yd0 = win.canvas.machine_origin()
    assert pos0 == pytest.approx((0.0, h))
    assert (xd0, yd0) == ((1.0, 0.0), (0.0, -1.0))
    marker0 = win.canvas.pen_marker()
    assert marker0 == pytest.approx((50.0, h - 60.0))

    win.machine_panel.swap_xy_check.setChecked(True)   # 触发 axisOptionsChanged
    win._on_machine_config_changed()
    pos1, xd1, yd1 = win.canvas.machine_origin()
    assert pos1 == pytest.approx((0.0, h))             # 原点不挪
    assert (round(xd1[0]), round(xd1[1])) == (0.0, -1.0)   # X 箭头沿左边向下
    assert (round(yd1[0]), round(yd1[1])) == (1.0, 0.0)    # Y 箭头沿顶边向右
    # 标记钉在原纸面位置 (50, h−60)：机器坐标换算为新映射的描述
    assert win.canvas.pen_marker() == pytest.approx((50.0, h - 60.0))
    assert win.machine_panel.start_point.pen_marker != (50.0, 60.0)
    win.close()


def test_build_gcode_with_swap_and_start_marker(qapp):
    """对调 + 起点标记：内容锚点与首条 G0 都落在标记的机器坐标。"""
    win = _win(qapp)
    doc = Document()
    doc.add(make_static_object([make_rect(0, 0, 10, 10)]))
    win.controller.set_document(doc)
    win.machine_panel.swap_xy_check.setChecked(True)
    win.machine_panel.set_start_point("canvas", 100.0, 50.0)   # 机器坐标
    r = win._build_gcode()
    text = "\n".join(r.lines)
    # 首条 G0 停在标记的机器坐标
    assert "G0 X100 Y50" in text
    # 内容页面左下角 (0,0) → 对调映射 (210,0) → 起点偏移把它平移到 (100,50)，
    # 矩形（页面 0..10）随之落在 x 90..100、y 50..60
    assert "X100 Y50" in text
    assert "X90 Y60" in text
    win.close()
