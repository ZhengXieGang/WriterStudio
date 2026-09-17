"""端到端集成演练（P13 全面审计收尾）。

覆盖完整用户链路：
    * 文本（字符覆盖 + 扰动 + 字距）与 Markdown（字距）对象；
    * 行内编辑提交 → 撤销/重做；
    * 项目存档 → 重新载入 → 按源再生成 → G-code 生成；
    * 静态对象笔画编辑与 base_strokes 同步（扰动后编辑不回退）；
    * 高级拾取起点与手动标记行为一致。
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from writerstudio.content.builder import make_markdown_object  # noqa: E402
from writerstudio.content.markdown import MarkdownStyle  # noqa: E402
from writerstudio.core.document import make_static_object  # noqa: E402
from writerstudio.core.sample import make_rect  # noqa: E402
from writerstudio.core.strokes import Stroke  # noqa: E402
from writerstudio.fonts.builder import TextSpec, make_text_object  # noqa: E402
from writerstudio.perturb.apply import BASE_STROKES_KEY, apply_perturb  # noqa: E402
from writerstudio.perturb.params import PerturbParams  # noqa: E402
from writerstudio.project import (  # noqa: E402
    ProjectData,
    load_project,
    regenerate_all,
    save_project,
)
from writerstudio.ui.main_window import MainWindow  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


def test_flow_edit_save_load_gcode(qapp, tmp_path):
    win = MainWindow()
    win.confirm_on_close = False

    # 1) 文本对象：字符覆盖 + 自然扰动 + 字距
    spec = TextSpec(text="AB-12", font_names=["futural"], size=12.0,
                    char_spacing=1.0,
                    char_overrides={"1": {"font": "futuram", "scale": 1.2}},
                    perturb=PerturbParams.natural(12.0, seed=5))
    obj = make_text_object(spec, win.font_manager)
    assert obj.local_strokes
    win.controller.add_object(obj)

    # 2) Markdown 对象：字距进嵌套样式
    md = make_markdown_object(
        "| a | b |\n| --- | --- |\n| 1 | 2 |",
        win.font_manager, style=MarkdownStyle(size=5.0, char_spacing=0.5))
    assert md.source.data["style"]["char_spacing"] == 0.5
    win.controller.add_object(md)

    # 3) 画布文字会话提交（文字变化，覆盖保留）——逐键即时提交，
    #    finish 只做收尾（幂等）
    win.canvas.sync_scene()
    win.canvas.select_object(obj)
    win._start_text_edit(obj)
    ts = win._text_session
    ts.select_all()
    ts.insert_text("AB-13")            # 全选替换（覆盖保留）
    ts.finish()                        # 结束编辑（幂等）
    spec_now = TextSpec.from_data(obj.source.data)
    assert spec_now.text == "AB-13"
    assert spec_now.char_overrides["1"]["font"] == "futuram"

    # 4) 撤销/重做可用
    win.controller.undo()
    win.controller.redo()
    assert TextSpec.from_data(obj.source.data).text == "AB-13"

    # 5) 存档 → 载入 → 按源再生成 → 关键字段与笔画都在
    project = ProjectData(
        document=win.controller.doc,
        machine_config=win.machine_panel.current_config(),
        start_point=win.machine_panel.current_start_point())
    path = tmp_path / "flow.wsproj"
    save_project(path, project)
    loaded = load_project(path)
    win2 = MainWindow()
    win2.confirm_on_close = False
    win2.controller.set_document(loaded.document)
    regenerate_all(loaded, win2.font_manager)
    win2.controller.set_document(loaded.document)

    texts = [o for o in loaded.document.objects if o.source.kind == "text"]
    mds = [o for o in loaded.document.objects if o.source.kind == "markdown"]
    assert len(texts) == 1 and len(mds) == 1
    s2 = TextSpec.from_data(texts[0].source.data)
    assert s2.text == "AB-13"
    assert s2.char_overrides["1"] == {"font": "futuram", "scale": 1.2}
    assert mds[0].source.data["style"]["char_spacing"] == 0.5
    assert texts[0].local_strokes and mds[0].local_strokes

    # 6) 载入后的文档能生成 G-code
    result = win2._build_gcode()
    assert any(ln.startswith(("G0", "G1")) for ln in result.lines)
    win.close()
    win2.close()


def test_stroke_edit_syncs_base_strokes(qapp):
    """静态对象：笔画编辑必须写回 base_strokes，扰动重算不得回退编辑。"""
    win = MainWindow()
    win.confirm_on_close = False
    obj = make_static_object([make_rect(0, 0, 20, 10)])
    win.controller.add_object(obj)
    apply_perturb(obj, PerturbParams(enabled=True, seed=3, line_wobble=0.4),
                  win.font_manager)
    assert BASE_STROKES_KEY in obj.source.data

    # 笔画编辑：把第一条笔画挪到远处
    win._commit_stroke_edit(obj, 0,
                            Stroke([(100.0, 100.0), (110.0, 100.0)], False))
    # 再扰动（从基线重算）：编辑必须保留，而不是退回原矩形 (0,0) 附近
    apply_perturb(obj, PerturbParams(enabled=True, seed=4, line_wobble=0.4),
                  win.font_manager)
    assert obj.local_strokes[0].bbox().x0 > 50

    # 删除笔画同样同步基线
    n_base = len(obj.source.data[BASE_STROKES_KEY])
    n_strokes = len(obj.local_strokes)
    win._delete_stroke(obj, 0)
    assert len(obj.source.data[BASE_STROKES_KEY]) == n_base - 1
    assert len(obj.local_strokes) == n_strokes - 1
    win.close()


def test_advanced_pick_sets_marker(qapp):
    """画布标记与手动标记同路径：落标记 + 起点模式 = 画布指定点。"""
    from writerstudio.machine.start_point import MODE_CANVAS
    win = MainWindow()
    win.confirm_on_close = False
    win._on_start_marked(30.0, 40.0)
    page = win.controller.doc.page
    assert win.machine_panel.start_point.pen_marker == pytest.approx(
        (30.0, page.height - 40.0))
    assert win.machine_panel.start_point.mode == MODE_CANVAS
    win.close()
